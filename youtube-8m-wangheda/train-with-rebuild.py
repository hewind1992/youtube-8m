# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Binary for training Tensorflow models on the YouTube-8M dataset."""

import json
import os
import time
import numpy

import eval_util
import losses
import frame_level_models
import video_level_models
import data_augmentation
import feature_transform
import readers
import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow import app
from tensorflow import flags
from tensorflow import gfile
from tensorflow import logging
import utils

FLAGS = flags.FLAGS

if __name__ == "__main__":
  # Dataset flags.
  flags.DEFINE_string("train_dir", "/tmp/yt8m_model/",
                      "The directory to save the model files in.")
  flags.DEFINE_string(
      "train_data_pattern", "",
      "File glob for the training dataset. If the files refer to Frame Level "
      "features (i.e. tensorflow.SequenceExample), then set --reader_type "
      "format. The (Sequence)Examples are expected to have 'rgb' byte array "
      "sequence feature as well as a 'labels' int64 context feature.")
  flags.DEFINE_string("feature_names", "mean_rgb", "Name of the feature "
                      "to use for training.")
  flags.DEFINE_string("feature_sizes", "1024", "Length of the feature vectors.")

  # Distillation flags
  flags.DEFINE_bool(
      "distillation_features", False,
      "If set, *DistillationFeatureReader will be used, the feature must contains"
      "prediction features (shape = [4716]).")
  flags.DEFINE_integer(
      "distillation_type", 0, "Type of distillation, options are 1 and 2.")
  flags.DEFINE_float("distillation_percent", 0.0,
                     "If larger than 0, final_loss = distillation_loss * percent + normal_loss * (1.0 - percent).")

  # Model flags.
  flags.DEFINE_bool(
      "frame_features", False,
      "If set, then --train_data_pattern must be frame-level features. "
      "Otherwise, --train_data_pattern must be aggregated video-level "
      "features. The model must also be set appropriately (i.e. to read 3D "
      "batches VS 4D batches.")
  flags.DEFINE_string(
      "model", "LogisticModel",
      "Which architecture to use for the model. Models are defined "
      "in models.py.")
  flags.DEFINE_bool(
      "multitask", False,
      "Whether to consider support_predictions")
  flags.DEFINE_bool(
      "start_new_model", False,
      "If set, this will not resume from a checkpoint and will instead create a"
      " new model instance.")

  # Training flags.
  flags.DEFINE_integer("batch_size", 1024,
                       "How many examples to process per batch for training.")
  flags.DEFINE_string("label_loss", "CrossEntropyLoss",
                      "Which loss function to use for training the model.")
  flags.DEFINE_float(
      "regularization_penalty", 1,
      "How much weight to give to the regularization loss (the label loss has "
      "a weight of 1).")
  flags.DEFINE_float("base_learning_rate", 0.01,
                     "Which learning rate to start with.")
  flags.DEFINE_float("learning_rate_decay", 0.95,
                     "Learning rate decay factor to be applied every "
                     "learning_rate_decay_examples.")
  flags.DEFINE_float("learning_rate_decay_examples", 4000000,
                     "Multiply current learning rate by learning_rate_decay "
                     "every learning_rate_decay_examples.")
  flags.DEFINE_integer("num_epochs", 5,
                       "How many passes to make over the dataset before "
                       "halting training.")
  flags.DEFINE_integer("max_steps", None,
                       "How many steps before stop.")
  flags.DEFINE_float("keep_checkpoint_every_n_hours", 1.0,
                     "How many hours before saving a new checkpoint")

  flags.DEFINE_bool("reweight", False,
                    "Whether to load model weight from file.")
  flags.DEFINE_string("sample_vocab_file", "",
                      "Where to load video_id vocabulary.")
  flags.DEFINE_string("sample_freq_file", "",
                      "Where to load sample frequency.")
 
  # Other flags.
  flags.DEFINE_integer("num_readers", 8,
                       "How many threads to use for reading input files.")
  flags.DEFINE_string("optimizer", "AdamOptimizer",
                      "What optimizer class to use.")
  flags.DEFINE_float("clip_gradient_norm", 1.0, "Norm to clip gradients to.")
  flags.DEFINE_bool(
      "log_device_placement", False,
      "Whether to write the device on which every op will run into the "
      "logs on startup.")
  flags.DEFINE_integer("recall_at_n", 100,
                       "N in recall@N.")
  flags.DEFINE_bool(
      "dropout", False,
      "Whether to consider dropout")
  flags.DEFINE_float("keep_prob", 1.0, 
      "probability to keep output (used in dropout, keep it unchanged in validationg and test)")
  flags.DEFINE_float("noise_level", 0.0, 
      "standard deviation of noise (added to hidden nodes)")

def validate_class_name(flag_value, category, modules, expected_superclass):
  """Checks that the given string matches a class of the expected type.

  Args:
    flag_value: A string naming the class to instantiate.
    category: A string used further describe the class in error messages
              (e.g. 'model', 'reader', 'loss').
    modules: A list of modules to search for the given class.
    expected_superclass: A class that the given class should inherit from.

  Raises:
    FlagsError: If the given class could not be found or if the first class
    found with that name doesn't inherit from the expected superclass.

  Returns:
    True if a class was found that matches the given constraints.
  """
  candidates = [getattr(module, flag_value, None) for module in modules]
  for candidate in candidates:
    if not candidate:
      continue
    if not issubclass(candidate, expected_superclass):
      raise flags.FlagsError("%s '%s' doesn't inherit from %s." %
                             (category, flag_value,
                              expected_superclass.__name__))
    return True
  raise flags.FlagsError("Unable to find %s '%s'." % (category, flag_value))

def get_input_data_tensors(reader,
                           data_pattern,
                           batch_size=1000,
                           num_epochs=None,
                           num_readers=1):
  """Creates the section of the graph which reads the training data.

  Args:
    reader: A class which parses the training data.
    data_pattern: A 'glob' style path to the data files.
    batch_size: How many examples to process at a time.
    num_epochs: How many passes to make over the training data. Set to 'None'
                to run indefinitely.
    num_readers: How many I/O threads to use.

  Returns:
    A tuple containing the features tensor, labels tensor, and optionally a
    tensor containing the number of frames per video. The exact dimensions
    depend on the reader being used.

  Raises:
    IOError: If no files matching the given pattern were found.
  """
  logging.info("Using batch size of " + str(batch_size) + " for training.")
  with tf.name_scope("train_input"):
    files = gfile.Glob(data_pattern)
    if not files:
      raise IOError("Unable to find training files. data_pattern='" +
                    data_pattern + "'.")
    logging.info("Number of training files: %s.", str(len(files)))
    filename_queue = tf.train.string_input_producer(
        files, num_epochs=num_epochs, shuffle=True)
    training_data = [
        reader.prepare_reader(filename_queue) for _ in range(num_readers)
    ]

    return tf.train.shuffle_batch_join(
        training_data,
        batch_size=batch_size,
        capacity=FLAGS.batch_size * 8,
        min_after_dequeue=FLAGS.batch_size,
        allow_smaller_final_batch=True,
        enqueue_many=True)


def find_class_by_name(name, modules):
  """Searches the provided modules for the named class and returns it."""
  modules = [getattr(module, name, None) for module in modules]
  return next(a for a in modules if a)

def get_video_weights_array():
  weight_lines = open(FLAGS.sample_freq_file).readlines()
  weights = numpy.array(map(float, weight_lines))
  weights = weights.reshape([len(weight_lines)])
  return weights, len(weight_lines)

def optional_assign_weights(sess, weights_input, weights_assignment):
  if weights_input is not None:
    weights, length = get_video_weights_array()
    _ = sess.run(weights_assignment, feed_dict={weights_input: weights})
    print "Assigned weights from %s" % FLAGS.sample_freq_file
  else:
    print "Collection weights_input not found"

def get_video_weights(video_id_batch):
  video_id_to_index = tf.contrib.lookup.string_to_index_table_from_file(
                          vocabulary_file=FLAGS.sample_vocab_file, default_value=0)
  indexes = video_id_to_index.lookup(video_id_batch)
  weights, length = get_video_weights_array()
  weights_input = tf.placeholder(tf.float32, shape=[length], name="sample_weights_input")
  weights_tensor = tf.get_variable("sample_weights",
                               shape=[length],
                               trainable=False,
                               dtype=tf.float32,
                               initializer=tf.constant_initializer(weights))
  weights_assignment = tf.assign(weights_tensor, weights_input)

  tf.add_to_collection("weights_input", weights_input)
  tf.add_to_collection("weights_assignment", weights_assignment)

  video_weight_batch = tf.nn.embedding_lookup(weights_tensor, indexes)
  return video_weight_batch

def build_graph(reader,
                model,
                train_data_pattern,
                label_loss_fn=losses.CrossEntropyLoss(),
                batch_size=1000,
                base_learning_rate=0.01,
                learning_rate_decay_examples=1000000,
                learning_rate_decay=0.95,
                optimizer_class=tf.train.AdamOptimizer,
                transformer_class=feature_transform.DefaultTransformer,
                augmenter_class=data_augmentation.DefaultAugmenter,
                clip_gradient_norm=1.0,
                regularization_penalty=1,
                num_readers=1,
                num_epochs=None):
  """Creates the Tensorflow graph.

  This will only be called once in the life of
  a training model, because after the graph is created the model will be
  restored from a meta graph file rather than being recreated.

  Args:
    reader: The data file reader. It should inherit from BaseReader.
    model: The core model (e.g. logistic or neural net). It should inherit
           from BaseModel.
    train_data_pattern: glob path to the training data files.
    label_loss_fn: What kind of loss to apply to the model. It should inherit
                from BaseLoss.
    batch_size: How many examples to process at a time.
    base_learning_rate: What learning rate to initialize the optimizer with.
    optimizer_class: Which optimization algorithm to use.
    clip_gradient_norm: Magnitude of the gradient to clip to.
    regularization_penalty: How much weight to give the regularization loss
                            compared to the label loss.
    num_readers: How many threads to use for I/O operations.
    num_epochs: How many passes to make over the data. 'None' means an
                unlimited number of passes.
  """
  
  global_step = tf.Variable(0, trainable=False, name="global_step")
  
  learning_rate = tf.train.exponential_decay(
      base_learning_rate,
      global_step * batch_size,
      learning_rate_decay_examples,
      learning_rate_decay,
      staircase=True)
  tf.summary.scalar('learning_rate', learning_rate)

  optimizer = optimizer_class(learning_rate)
  if FLAGS.distillation_features:
    video_id, model_input_raw, labels_batch, num_frames, distill_labels_batch = (
        get_input_data_tensors(
            reader,
            train_data_pattern,
            batch_size=batch_size,
            num_readers=num_readers,
            num_epochs=num_epochs))
    if FLAGS.distillation_features and FLAGS.distillation_type == 2:
      p = FLAGS.distillation_percent
      print "distillation_percent =", p, "reforming labels"
      float_labels = tf.cast(labels_batch, dtype=tf.float32)
      sum_float_labels = tf.reduce_sum(float_labels, axis=1, keep_dims=True)
      sum_distill_labels = tf.reduce_sum(distill_labels_batch, axis=1, keep_dims=True) + 1e-6
      distill_labels_batch = float_labels + distill_labels_batch * (sum_float_labels / sum_distill_labels * p)
      distill_labels_batch = tf.clip_by_value(distill_labels_batch, clip_value_min=0.0, clip_value_max=1.0)
  else:
    video_id, model_input_raw, labels_batch, num_frames = (
        get_input_data_tensors(
            reader,
            train_data_pattern,
            batch_size=batch_size,
            num_readers=num_readers,
            num_epochs=num_epochs))

  # data augmentation, will not persist in inference
  data_augmenter = augmenter_class()
  model_input_raw, labels_batch, num_frames = data_augmenter.augment(model_input_raw, num_frames=num_frames, labels_batch=labels_batch)

  tf.summary.histogram("model/input_raw", model_input_raw)

  feature_transformer = transformer_class()
  model_input, num_frames = feature_transformer.transform(model_input_raw, num_frames=num_frames)

  tf.summary.histogram("model/input", model_input)

  with tf.name_scope("model"):
    if FLAGS.noise_level > 0:
      noise_level_tensor = tf.placeholder_with_default(0.0, shape=[], name="noise_level")
    else:
      noise_level_tensor = None

    if FLAGS.dropout:
      keep_prob_tensor = tf.placeholder_with_default(1.0, shape=[], name="keep_prob")
      result = model.create_model(
          model_input,
          num_frames=num_frames,
          vocab_size=reader.num_classes,
          labels=labels_batch,
          dropout=FLAGS.dropout,
          keep_prob=keep_prob_tensor,
          noise_level=noise_level_tensor)
    else:
      result = model.create_model(
          model_input,
          num_frames=num_frames,
          vocab_size=reader.num_classes,
          labels=labels_batch,
          noise_level=noise_level_tensor)

    for variable in slim.get_model_variables():
      tf.summary.histogram(variable.op.name, variable)

    print "result", result
    predictions = result["predictions"]
    if "loss" in result.keys():
      label_loss = result["loss"]
    else:
      video_weights_batch = None
      if FLAGS.reweight:
        video_weights_batch = get_video_weights(video_id)
      if FLAGS.multitask:
        support_predictions = result["support_predictions"]
        tf.summary.histogram("model/support_predictions", support_predictions)
        print "support_predictions", support_predictions
        if FLAGS.distillation_features and FLAGS.distillation_type == 1:
          p = FLAGS.distillation_percent
          print "distillation_percent =", p
          if p <= 0:
            label_loss = label_loss_fn.calculate_loss(predictions, support_predictions, labels_batch, weights=video_weights_batch)
          elif p >= 1:
            label_loss = label_loss_fn.calculate_loss(predictions, support_predictions, distill_labels_batch, weights=video_weights_batch)
          else:
            label_loss = label_loss_fn.calculate_loss(predictions, support_predictions, labels_batch, weights=video_weights_batch) * (1.0 - p) \
                        + label_loss_fn.calculate_loss(predictions, support_predictions, distill_labels_batch, weights=video_weights_batch) * p
        elif FLAGS.distillation_features and FLAGS.distillation_type == 2:
          print "using pure distillation loss"
          label_loss = label_loss_fn.calculate_loss(predictions, support_predictions, distill_labels_batch, weights=video_weights_batch)
        else:
          print "using original loss"
          label_loss = label_loss_fn.calculate_loss(predictions, support_predictions, labels_batch, weights=video_weights_batch)
      else:
        if FLAGS.distillation_features and FLAGS.distillation_type == 1:
          p = FLAGS.distillation_percent
          print "distillation_percent =", p
          if p <= 0:
            label_loss = label_loss_fn.calculate_loss(predictions, labels_batch, weights=video_weights_batch)
          elif p >= 1:
            label_loss = label_loss_fn.calculate_loss(predictions, distill_labels_batch, weights=video_weights_batch)
          else:
            label_loss = label_loss_fn.calculate_loss(predictions, labels_batch, weights=video_weights_batch) * (1.0 - p) \
                         + label_loss_fn.calculate_loss(predictions, distill_labels_batch, weights=video_weights_batch) * p
        elif FLAGS.distillation_features and FLAGS.distillation_type == 2:
          label_loss = label_loss_fn.calculate_loss(predictions, distill_labels_batch, weights=video_weights_batch)
        else:
          label_loss = label_loss_fn.calculate_loss(predictions, labels_batch, weights=video_weights_batch)

    tf.summary.histogram("model/predictions", predictions)
    tf.summary.scalar("label_loss", label_loss)

    if "regularization_loss" in result.keys():
      reg_loss = result["regularization_loss"]
    else:
      reg_loss = tf.constant(0.0)
    
    reg_losses = tf.losses.get_regularization_losses()
    if reg_losses:
      reg_loss += tf.add_n(reg_losses)
    
    if regularization_penalty != 0:
      tf.summary.scalar("reg_loss", reg_loss)

    # Adds update_ops (e.g., moving average updates in batch normalization) as
    # a dependency to the train_op.
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    if "update_ops" in result.keys():
      update_ops += result["update_ops"]
    if update_ops:
      with tf.control_dependencies(update_ops):
        barrier = tf.no_op(name="gradient_barrier")
        with tf.control_dependencies([barrier]):
          label_loss = tf.identity(label_loss)

    # Incorporate the L2 weight penalties etc.
    final_loss = regularization_penalty * reg_loss + label_loss

    gradients = optimizer.compute_gradients(final_loss,
        colocate_gradients_with_ops=False)
    if clip_gradient_norm > 0:
      with tf.name_scope('clip_grads'):
        gradients = utils.clip_gradient_norms(gradients , clip_gradient_norm)
    train_op = optimizer.apply_gradients(gradients, global_step=global_step)

    tf.add_to_collection("global_step", global_step)
    tf.add_to_collection("loss", label_loss)
    tf.add_to_collection("predictions", predictions)
    tf.add_to_collection("input_batch_raw", model_input_raw)
    tf.add_to_collection("input_batch", model_input)
    tf.add_to_collection("num_frames", num_frames)
    tf.add_to_collection("labels", tf.cast(labels_batch, tf.float32))
    tf.add_to_collection("train_op", train_op)
    if FLAGS.dropout:
      tf.add_to_collection("keep_prob", keep_prob_tensor)
    if FLAGS.noise_level > 0:
      tf.add_to_collection("noise_level", noise_level_tensor)


class Trainer(object):
  """A Trainer to train a Tensorflow graph."""

  def __init__(self, cluster, task, train_dir, log_device_placement=True):
    """"Creates a Trainer.

    Args:
      cluster: A tf.train.ClusterSpec if the execution is distributed.
        None otherwise.
      task: A TaskSpec describing the job type and the task index.
    """

    self.cluster = cluster
    self.task = task
    self.is_master = (task.type == "master" and task.index == 0)
    self.train_dir = train_dir
    self.config = tf.ConfigProto(log_device_placement=log_device_placement)

    if self.is_master and self.task.index > 0:
      raise StandardError("%s: Only one replica of master expected",
                          task_as_string(self.task))

  def run(self, start_new_model=False):
    """Performs training on the currently defined Tensorflow graph.

    Returns:
      A tuple of the training Hit@1 and the training PERR.
    """
    if self.is_master and start_new_model:
      self.remove_training_directory(self.train_dir)

    target, device_fn = self.start_server_if_distributed()

    checkpoint = self.get_latest_checkpoint(start_new_model, self.train_dir)

    with tf.Graph().as_default() as graph:

      with tf.device(device_fn):

        saver = self.build_model()

        global_step = tf.get_collection("global_step")[0]
        loss = tf.get_collection("loss")[0]
        predictions = tf.get_collection("predictions")[0]
        labels = tf.get_collection("labels")[0]
        train_op = tf.get_collection("train_op")[0]
        init_op = tf.global_variables_initializer()

        if FLAGS.dropout:
          keep_prob_tensor = tf.get_collection("keep_prob")[0]
        if FLAGS.noise_level > 0:
          noise_level_tensor = tf.get_collection("noise_level")[0]
        if FLAGS.reweight:
          weights_input, weights_assignment = None, None
          if len(tf.get_collection("weights_input")) > 0:
            weights_input = tf.get_collection("weights_input")[0]
            weights_assignment = tf.get_collection("weights_assignment")[0]

    sv = tf.train.Supervisor(
        graph,
        logdir=self.train_dir,
        init_op=init_op,
        is_chief=self.is_master,
        global_step=global_step,
        save_model_secs=15 * 60,
        save_summaries_secs=120,
        saver=saver)

    logging.info("%s: Starting managed session.", task_as_string(self.task))
    with sv.managed_session(target, config=self.config) as sess:

      if checkpoint is not None:
        saver.restore(sess, checkpoint)

      # re-assign weights
      if FLAGS.reweight:
        optional_assign_weights(sess, weights_input, weights_assignment)

      steps = 0
      try:
        logging.info("%s: Entering training loop.", task_as_string(self.task))
        while not sv.should_stop():

          steps += 1
          batch_start_time = time.time()
          custom_feed = {}
          if FLAGS.dropout:
            custom_feed[keep_prob_tensor] = FLAGS.keep_prob
          if FLAGS.noise_level > 0:
            custom_feed[noise_level_tensor] = FLAGS.noise_level

          _, global_step_val, loss_val, predictions_val, labels_val = sess.run(
              [train_op, global_step, loss, predictions, labels], feed_dict=custom_feed)
          seconds_per_batch = time.time() - batch_start_time

          if self.is_master:
            examples_per_second = labels_val.shape[0] / seconds_per_batch
            hit_at_one = eval_util.calculate_hit_at_one(predictions_val,
                                                        labels_val)
            perr = eval_util.calculate_precision_at_equal_recall_rate(
                predictions_val, labels_val)
            recall = "N/A"
            if False:
              recall = eval_util.calculate_recall_at_n(
                  predictions_val, labels_val, FLAGS.recall_at_n)
              sv.summary_writer.add_summary(
                  utils.MakeSummary("model/Training_Recall@%d" % FLAGS.recall_at_n, recall), global_step_val)
              recall = "%.2f" % recall
            gap = eval_util.calculate_gap(predictions_val, labels_val)

            logging.info(
                "%s: training step " + str(global_step_val) + "| Hit@1: " +
                ("%.2f" % hit_at_one) + " PERR: " + ("%.2f" % perr) + " GAP: " +
                ("%.2f" % gap) + " Recall@%d: " % FLAGS.recall_at_n +
                recall + " Loss: " + str(loss_val),
                task_as_string(self.task))

            sv.summary_writer.add_summary(
                utils.MakeSummary("model/Training_Hit@1", hit_at_one),
                global_step_val)
            sv.summary_writer.add_summary(
                utils.MakeSummary("model/Training_Perr", perr), global_step_val)
            sv.summary_writer.add_summary(
                utils.MakeSummary("model/Training_GAP", gap), global_step_val)
            sv.summary_writer.add_summary(
                utils.MakeSummary("global_step/Examples/Second",
                                  examples_per_second), global_step_val)
            sv.summary_writer.flush()

          if FLAGS.max_steps is not None and steps > FLAGS.max_steps:
            logging.info("%s: Done training -- max_steps limit reached.",
                         task_as_string(self.task))
            break

      except tf.errors.OutOfRangeError:
        logging.info("%s: Done training -- epoch limit reached.",
                     task_as_string(self.task))

    logging.info("%s: Exited training loop.", task_as_string(self.task))
    sv.Stop()

  def start_server_if_distributed(self):
    """Starts a server if the execution is distributed."""

    if self.cluster:
      logging.info("%s: Starting trainer within cluster %s.",
                   task_as_string(self.task), self.cluster.as_dict())
      server = start_server(self.cluster, self.task)
      target = server.target
      device_fn = tf.train.replica_device_setter(
          ps_device="/job:ps",
          worker_device="/job:%s/task:%d" % (self.task.type, self.task.index),
          cluster=self.cluster)
    else:
      target = ""
      device_fn = ""
    return (target, device_fn)

  def remove_training_directory(self, train_dir):
    """Removes the training directory."""
    try:
      logging.info(
          "%s: Removing existing train directory.",
          task_as_string(self.task))
      gfile.DeleteRecursively(train_dir)
    except:
      logging.error(
          "%s: Failed to delete directory " + train_dir +
          " when starting a new model. Please delete it manually and" +
          " try again.", task_as_string(self.task))

  def get_latest_checkpoint(self, start_new_model, train_dir):
    if start_new_model:
      logging.info("%s: Flag 'start_new_model' is set. Building a new model.",
                   task_as_string(self.task))
      return None
    
    latest_checkpoint = tf.train.latest_checkpoint(train_dir)
    if not latest_checkpoint: 
      logging.info("%s: No checkpoint file found. Building a new model.",
                   task_as_string(self.task))
      return None
    
    return latest_checkpoint

  def build_model(self):
    """Find the model and build the graph."""

    # Convert feature_names and feature_sizes to lists of values.
    feature_names, feature_sizes = utils.GetListOfFeatureNamesAndSizes(
        FLAGS.feature_names, FLAGS.feature_sizes)

    if FLAGS.distillation_features:
      if FLAGS.frame_features:
        reader = readers.YT8MFrameDistillationFeatureReader(
            feature_names=feature_names, feature_sizes=feature_sizes)
      else:
        reader = readers.YT8MAggregatedDistillationFeatureReader(
            feature_names=feature_names, feature_sizes=feature_sizes)
    else:
      if FLAGS.frame_features:
        reader = readers.YT8MFrameFeatureReader(
            feature_names=feature_names, feature_sizes=feature_sizes)
      else:
        reader = readers.YT8MAggregatedFeatureReader(
            feature_names=feature_names, feature_sizes=feature_sizes)

    # Find the model.
    model = find_class_by_name(FLAGS.model,
                               [frame_level_models, video_level_models])()
    label_loss_fn = find_class_by_name(FLAGS.label_loss, [losses])()
    optimizer_class = find_class_by_name(FLAGS.optimizer, [tf.train])
    transformer_class = find_class_by_name(FLAGS.feature_transformer, [feature_transform])
    augmenter_class = find_class_by_name(FLAGS.data_augmenter, [data_augmentation])

    build_graph(reader=reader,
                 model=model,
                 optimizer_class=optimizer_class,
                 augmenter_class=augmenter_class,
                 transformer_class=transformer_class,
                 clip_gradient_norm=FLAGS.clip_gradient_norm,
                 train_data_pattern=FLAGS.train_data_pattern,
                 label_loss_fn=label_loss_fn,
                 base_learning_rate=FLAGS.base_learning_rate,
                 learning_rate_decay=FLAGS.learning_rate_decay,
                 learning_rate_decay_examples=FLAGS.learning_rate_decay_examples,
                 regularization_penalty=FLAGS.regularization_penalty,
                 num_readers=FLAGS.num_readers,
                 batch_size=FLAGS.batch_size,
                 num_epochs=FLAGS.num_epochs)

    logging.info("%s: Built graph.", task_as_string(self.task))

    return tf.train.Saver(max_to_keep=3, keep_checkpoint_every_n_hours=FLAGS.keep_checkpoint_every_n_hours)


class ParameterServer(object):
  """A parameter server to serve variables in a distributed execution."""

  def __init__(self, cluster, task):
    """Creates a ParameterServer.

    Args:
      cluster: A tf.train.ClusterSpec if the execution is distributed.
        None otherwise.
      task: A TaskSpec describing the job type and the task index.
    """

    self.cluster = cluster
    self.task = task

  def run(self):
    """Starts the parameter server."""

    logging.info("%s: Starting parameter server within cluster %s.",
                 task_as_string(self.task), self.cluster.as_dict())
    server = start_server(self.cluster, self.task)
    server.join()


def start_server(cluster, task):
  """Creates a Server.

  Args:
    cluster: A tf.train.ClusterSpec if the execution is distributed.
      None otherwise.
    task: A TaskSpec describing the job type and the task index.
  """

  if not task.type:
    raise ValueError("%s: The task type must be specified." %
                     task_as_string(task))
  if task.index is None:
    raise ValueError("%s: The task index must be specified." %
                     task_as_string(task))

  # Create and start a server.
  return tf.train.Server(
      tf.train.ClusterSpec(cluster),
      protocol="grpc",
      job_name=task.type,
      task_index=task.index)

def task_as_string(task):
  return "/job:%s/task:%s" % (task.type, task.index)

def main(unused_argv):
  # Load the environment.
  env = json.loads(os.environ.get("TF_CONFIG", "{}"))

  # Load the cluster data from the environment.
  cluster_data = env.get("cluster", None)
  cluster = tf.train.ClusterSpec(cluster_data) if cluster_data else None

  # Load the task data from the environment.
  task_data = env.get("task", None) or {"type": "master", "index": 0}
  task = type("TaskSpec", (object,), task_data)

  # Logging the version.
  logging.set_verbosity(tf.logging.INFO)
  logging.info("%s: Tensorflow version: %s.",
               task_as_string(task), tf.__version__)

  # Dispatch to a master, a worker, or a parameter server.
  if not cluster or task.type == "master" or task.type == "worker":
    Trainer(cluster, task, FLAGS.train_dir, FLAGS.log_device_placement).run(
        start_new_model=FLAGS.start_new_model)
  elif task.type == "ps":
    ParameterServer(cluster, task).run()
  else:
    raise ValueError("%s: Invalid task_type: %s." %
                     (task_as_string(task), task.type))


if __name__ == "__main__":
  app.run()
