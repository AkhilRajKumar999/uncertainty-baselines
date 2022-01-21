# coding=utf-8
# Copyright 2022 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ensembles of ResNet50 MC Dropout models (Gal and Ghahramani 2016, Smith and Gal 2018) on Kaggle's Diabetic Retinopathy Detection dataset.

This script only performs evaluation, not training. We recommend training
ensembles by launching independent runs of `dropout.py` over different
seeds.
"""

import os

from absl import app
from absl import flags
from absl import logging
import numpy as np
import robustness_metrics as rm
import tensorflow as tf
import tensorflow_datasets as tfds
import uncertainty_baselines as ub
import utils  # local file import

# Data load / output flags.
flags.DEFINE_string(
    'checkpoint_dir', '/tmp/diabetic_retinopathy_detection/dropout',
    'The directory from which the trained dropout '
    'model weights are retrieved.')
flags.DEFINE_string(
    'output_dir', '/tmp/diabetic_retinopathy_detection/dropoutensemble',
    'The directory where the dropout ensemble model weights '
    'and training/evaluation summaries are stored.')
flags.DEFINE_string('data_dir', None, 'Path to training and testing data.')
flags.mark_flag_as_required('data_dir')

# General model flags.
flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('eval_batch_size', 32,
                     'The per-core validation/test batch size.')

# Metric flags.
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')

# Dropout-related flags -- should be consistent with the trained
# dropout models in the checkpoint dir above.
flags.DEFINE_float('dropout_rate', 0.1, 'Dropout rate, between [0.0, 1.0).')
flags.DEFINE_bool(
    'filterwise_dropout', False,
    'Dropout whole convolutional filters instead of individual '
    'values in the feature map.')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', True, 'Whether to run on GPU, otherwise CPU.')
flags.DEFINE_bool('use_bfloat16', False, 'Whether to use mixed precision.')
flags.DEFINE_integer(
    'num_cores', 1,
    'Number of TPU cores or number of GPUs - only support 1 GPU for now.')
FLAGS = flags.FLAGS


def main(argv):
  del argv  # unused arg
  tf.io.gfile.makedirs(FLAGS.output_dir)
  logging.info('Saving Ensemble MC Dropout predictions to %s', FLAGS.output_dir)
  tf.random.set_seed(FLAGS.seed)

  if FLAGS.num_cores > 1:
    raise ValueError('Only a single accelerator is currently supported.')

  if FLAGS.use_gpu:
    logging.info('Use GPU')
  else:
    logging.info('Use CPU')
    os.environ['CUDA_VISIBLE_DEVICES'] = '-1'

  # As per the Kaggle challenge, we have split sizes:
  # train: 35,126
  # validation: 10,906 (currently unused)
  # test: 42,670
  ds_info = tfds.builder('diabetic_retinopathy_detection').info
  eval_batch_size = FLAGS.eval_batch_size * FLAGS.num_cores
  steps_per_eval = ds_info.splits['test'].num_examples // eval_batch_size

  dataset_test_builder = ub.datasets.get(
      'diabetic_retinopathy_detection', split='test', data_dir=FLAGS.data_dir)
  dataset_test = dataset_test_builder.load(batch_size=eval_batch_size)

  if FLAGS.use_bfloat16:
    policy = tf.keras.mixed_precision.experimental.Policy('mixed_bfloat16')
    tf.keras.mixed_precision.experimental.set_policy(policy)

  # TODO(nband): debug, switch from keras.models.save to Checkpoint
  logging.info('Building Keras ResNet-50 Ensemble MC Dropout model')
  ensemble_filenames = utils.parse_keras_models(FLAGS.checkpoint_dir)

  ensemble_size = len(ensemble_filenames)
  logging.info('Ensemble size: %s', ensemble_size)
  logging.info('Ensemble Keras model dir names: %s', str(ensemble_filenames))

  # Write model predictions to files.
  for member, ensemble_filename in enumerate(ensemble_filenames):
    model = tf.keras.models.load_model(ensemble_filename, compile=False)
    logging.info('Model input shape: %s', model.input_shape)
    logging.info('Model output shape: %s', model.output_shape)
    logging.info('Model number of weights: %s', model.count_params())
    filename = f'{member}.npy'
    filename = os.path.join(FLAGS.output_dir, filename)
    if not tf.io.gfile.exists(filename):
      logits = []
      test_iterator = iter(dataset_test)
      for i in range(steps_per_eval):
        inputs = next(test_iterator)  # pytype: disable=attribute-error
        images = inputs['features']
        logits.append(model(images, training=False))
        if i % 100 == 0:
          logging.info(
              'Ensemble member %d/%d: Completed %d of %d eval steps.',
              member + 1,
              ensemble_size,
              i + 1,
              steps_per_eval)

      logits = tf.concat(logits, axis=0)
      with tf.io.gfile.GFile(filename, 'w') as f:
        np.save(f, logits.numpy())
    percent = (member + 1) / ensemble_size
    message = (
        '{:.1%} completion for prediction: ensemble member {:d}/{:d}.'.format(
            percent, member + 1, ensemble_size))
    logging.info(message)

  metrics = {
      'test/negative_log_likelihood': tf.keras.metrics.Mean(),
      'test/gibbs_cross_entropy': tf.keras.metrics.Mean(),
      'test/accuracy': tf.keras.metrics.BinaryAccuracy(),
      'test/auprc': tf.keras.metrics.AUC(curve='PR'),
      'test/auroc': tf.keras.metrics.AUC(curve='ROC'),
      'test/ece': rm.metrics.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
      'test/diversity': rm.metrics.AveragePairwiseDiversity(),
  }

  for i in range(ensemble_size):
    metrics['test/nll_member_{}'.format(i)] = tf.keras.metrics.Mean()
    metrics['test/accuracy_member_{}'.format(i)] = (
        tf.keras.metrics.BinaryAccuracy())

  # Evaluate model predictions.
  logits_dataset = []
  for member in range(ensemble_size):
    filename = f'{member}.npy'
    filename = os.path.join(FLAGS.output_dir, filename)
    with tf.io.gfile.GFile(filename, 'rb') as f:
      logits_dataset.append(np.load(f))

  logits_dataset = tf.convert_to_tensor(logits_dataset)
  test_iterator = iter(dataset_test)

  for step in range(steps_per_eval):
    inputs = next(test_iterator)  # pytype: disable=attribute-error
    labels = inputs['labels']
    logits = logits_dataset[:, (step * eval_batch_size):((step + 1) *
                                                         eval_batch_size)]
    labels = tf.cast(labels, tf.float32)
    logits = tf.cast(logits, tf.float32)
    negative_log_likelihood_metric = rm.metrics.EnsembleCrossEntropy(
        binary=True)
    negative_log_likelihood_metric.add_batch(
        logits, labels=tf.expand_dims(labels, axis=-1))
    negative_log_likelihood = list(
        negative_log_likelihood_metric.result().values())[0]
    per_probs = tf.nn.sigmoid(logits)
    probs = tf.reduce_mean(per_probs, axis=0)
    gibbs_ce_metric = rm.metrics.GibbsCrossEntropy(binary=True)
    gibbs_ce_metric.add_batch(logits, labels=tf.expand_dims(labels, axis=-1))
    gibbs_ce = list(gibbs_ce_metric.result().values())[0]
    metrics['test/negative_log_likelihood'].update_state(
        negative_log_likelihood)
    metrics['test/gibbs_cross_entropy'].update_state(gibbs_ce)
    metrics['test/accuracy'].update_state(labels, probs)
    metrics['test/auprc'].update_state(labels, probs)
    metrics['test/auroc'].update_state(labels, probs)
    metrics['test/ece'].add_batch(probs, label=labels)
    metrics['test/diversity'].add_batch(per_probs)

    for i in range(ensemble_size):
      member_probs = per_probs[i]
      member_loss = tf.keras.losses.binary_crossentropy(labels, member_probs)
      metrics['test/nll_member_{}'.format(i)].update_state(member_loss)
      metrics['test/accuracy_member_{}'.format(i)].update_state(
          labels, member_probs)

  total_results = {name: metric.result() for name, metric in metrics.items()}
  # Results from Robustness Metrics themselves return a dict, so flatten them.
  total_results = utils.flatten_dictionary(total_results)
  logging.info('Metrics: %s', total_results)


if __name__ == '__main__':
  app.run(main)
