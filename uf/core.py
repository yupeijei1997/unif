# coding:=utf-8
# Copyright 2020 Tencent. All rights reserved.
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
''' Core methods and class. '''

import os
import json
import collections
from abc import abstractmethod

from .tools import tf
from . import processing
from . import optimization
from . import utils



class BaseModule:
    ''' Parent class of all the application processors. '''

    def __init__(self, init_checkpoint, output_dir, gpu_ids):

        # read checkpoint path
        self.init_checkpoint = init_checkpoint

        # create output directory
        self.output_dir = output_dir
        if output_dir:
            tf.gfile.MakeDirs(output_dir)
            tf.logging.info('Output directory: %s' % output_dir)

        # convert GPU ids to list
        self._gpu_ids = []
        if gpu_ids is None:
            gpu_ids = os.environ.get('CUDA_VISIBLE_DEVICES', '')
        elif not gpu_ids:
            pass
        if gpu_ids:
            try:
                if isinstance(gpu_ids, str):
                    self._gpu_ids = gpu_ids.replace(' ', '').split(',')
                else:
                    self._gpu_ids = list(map(str, gpu_ids))
            except Exception:
                raise ValueError(
                    '`gpu_ids` should be a list of GPU ids or a string '
                    'seperated with commas.')

        # build graph
        self.graph = tf.Graph()

        # Before we build the graph, `score` and fast `predict`
        # is now allowed.
        self.step = 0
        self._graph_mode = None
        self._graph_built = False
        self._inited_vars = set()

    def reset(self):
        ''' Reset existing session and graph. '''
        try:
            self.sess.close()
        except AttributeError:
            pass

        # initialize graph and session
        self.graph = tf.Graph()

        # Before we build the graph, `score` and fast `predict`
        # is now allowed.
        self.step = 0
        self._graph_mode = None
        self._graph_built = False
        self._inited_vars = set()

    def __repr__(self):
        info = 'uf.' + self.__class__.__name__ + '('
        for key in self.__class__.__init__.__code__.co_varnames[1:]:
            try:
                value = self.__getattribute__(key)
            except:
                value = self.__init_args__[key]
            value = '\'%s\'' % value if isinstance(value, str) \
                else '%s' % value
            info += '%s=%s, ' % (key, value)
        return info[:-2] + ')'

    def __del__(self):
        try:
            self.sess.close()
        except Exception:
            pass

    def to_tfrecords(self, X=None, y=None, sample_weight=None,
                     X_tokenized=None, tfrecords_file=None):
        ''' Transform raw data and serialize into TFRecords.

        Args:
            X: list. A list object consisting untokenized inputs.
            y: list. A list object consisting labels.
            sample_weight: list. A list object of float-convertable values.
            X_tokenized: list. A list object consisting tokenized inputs.
              Either `X` or `X_tokenized` should be None.
            tfrecords_file: string. The file path of TFRecords to write.
        Returns:
            None
        '''

        if not tfrecords_file:
            if self.output_dir:
                tfrecords_file = os.path.join(
                    self.output_dir, 'train.tfrecords')
            else:
                tfrecords_file = './train.tfrecords'

        data = self._parallel_convert(
            X, y, sample_weight, X_tokenized, is_training=True)

        tf.logging.info('Serializing data into %s' % tfrecords_file)
        utils.write_tfrecords(data, tfrecords_file)

    def fit_from_tfrecords(
            self, batch_size=32,
            learning_rate=5e-5,
            target_steps=None,
            total_steps=-3,
            warmup_ratio=0.1,
            print_per_secs=0.1,
            save_per_steps=1000,
            tfrecords_files=None,
            n_jobs=None,
            **kwargs):
        ''' Training the model using TFRecords.

        Args:
            batch_size: int. The size of batch in each step.
            learning_rate: float. Peak learning rate during training process.
            target_steps: float/int. The number of target steps, must be
              smaller or equal to `total_steps`. When assigned to a negative
              value, the model automatically calculate the required steps to
              finish a loop which covers all training data, then the value is
              multiplied with the absolute value of `target_steps` to obtain
              the real target number of steps.
            total_steps: int. The number of total steps in optimization, must
              be larger or equal to `target_steps`. When assigned to a
              negative value, the model automatically calculate the required
              steps to finish a loop which covers all training data, then the
              value is multiplied with the absolute value of `total_steps` to
              obtain the real number of total steps.
            warmup_ratio: float. How much percentage of total steps fall into
              warming up stage.
            print_per_secs: int. How many steps to print training information,
              e.g. training loss.
            save_per_steps: int. How many steps to save model into checkpoint
              file. Valid only when `output_dir` is not None.
            tfrecords_files: list. A list object of string defining TFRecords
              files to read.
            n_jobs: int. Number of threads in processing tfrecords. Default
              to the number of cpu cores in the comping device.
            **kwargs: Other arguments about layer-wise learning rate decay,
              adversarial training or model-specific settings. See `README.md`
              to obtain more
        Returns:
            None
        '''

        # Make sure the arguments are correct.
        self.batch_size = batch_size
        if self._gpu_ids:
            assert batch_size % len(self._gpu_ids) == 0, (
                '`batch_size` should be evenly divided by the number of GPUs, '
                'but got %d and %d.'
                % (batch_size, len(self._gpu_ids)))

        # Get absolute path of tf.records file
        if not tfrecords_files:
            if self.output_dir:
                tfrecords_files = \
                    [os.path.join(self.output_dir, 'train.tfrecords')]
            else:
                tfrecords_files = ['train.tfrecords']
        elif isinstance(tfrecords_files, str):
            tfrecords_files = tfrecords_files.split(',')

        # Confirm the number of training steps and warmup
        # steps. In reality, we use a slanted learning rate
        # that starts to decay after gradually climing to
        # the pre-assigned peak level.
        n_inputs = utils.get_tfrecords_length(tfrecords_files)
        self.steps_per_epoch = (n_inputs - 1) // batch_size + 1
        if total_steps < 0:
            total_steps = -total_steps * self.steps_per_epoch
        self.total_steps = int(total_steps)
        if not target_steps:
            target_steps = self.total_steps
        elif target_steps < 0:
            target_steps = - target_steps * self.steps_per_epoch
        target_steps = int(target_steps)
        if target_steps > self.total_steps:
            raise ValueError('Target steps can\'t exceed total steps.')
        self.num_warmup_steps = int(self.total_steps * warmup_ratio)

        # Define optimization process, build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            self._global_step = optimization.get_global_step()
            self._optimizer = optimization.get_optimizer(
                init_lr=learning_rate,
                global_step=self._global_step,
                num_train_steps=self.total_steps,
                num_warmup_steps=self.num_warmup_steps,
                key_to_depths=self._key_to_depths,
                **kwargs)
            kwargs.update(tfrecords_files=tfrecords_files, n_jobs=n_jobs)
            return self._build('fit', **kwargs).run(
                target_steps,
                print_per_secs=print_per_secs,
                save_per_steps=save_per_steps)

    def fit(self, X=None, y=None, sample_weight=None, X_tokenized=None,
            batch_size=32,
            learning_rate=5e-5,
            target_steps=None,
            total_steps=-3,
            warmup_ratio=0.1,
            print_per_secs=0.1,
            save_per_steps=1000,
            **kwargs):
        ''' Training the model.

        Args:
            X: list. A list object consisting untokenized inputs.
            y: list. A list object consisting labels.
            sample_weight: list. A list object of float-convertable values.
            X_tokenized: list. A list object consisting tokenized inputs.
              Either `X` or `X_tokenized` should be None.
            batch_size: int. The size of batch in each step.
            learning_rate: float. Peak learning rate during training process.
            target_steps: float/int. The number of target steps, must be
              smaller or equal to `total_steps`. When assigned to a negative
              value, the model automatically calculate the required steps to
              finish a loop which covers all training data, then the value is
              multiplied with the absolute value of `target_steps` to obtain
              the real target number of steps.
            total_steps: int. The number of total steps in optimization, must
              be larger or equal to `target_steps`. When assigned to a
              negative value, the model automatically calculate the required
              steps to finish a loop which covers all training data, then the
              value is multiplied with the absolute value of `total_steps` to
              obtain the real number of total steps.
            warmup_ratio: float. How much percentage of total steps fall into
              warming up stage.
            print_per_secs: int. How many steps to print training information,
              e.g. training loss.
            save_per_steps: int. How many steps to save model into checkpoint
              file. Valid only when `output_dir` is not None.
            **kwargs: Other arguments about layer-wise learning rate decay,
              adversarial training or model-specific settings. See `README.md`
              to obtain more
        Returns:
            None
        '''

        # Make sure the arguments are correct.
        self.batch_size = batch_size
        if self._gpu_ids:
            assert batch_size % len(self._gpu_ids) == 0, (
                '`batch_size` should be evenly divided by the number of GPUs, '
                'but got %d and %d.'
                % (batch_size, len(self._gpu_ids)))

        # Convert raw data to structed data. This method
        # should be specifically implemented by child classes.
        self.data = self._parallel_convert(
            X, y, sample_weight, X_tokenized, is_training=True)

        # Confirm the number of training steps and warmup
        # steps. In reality, we use a slanted learning rate
        # that starts to decay after gradually climing to
        # the pre-assigned peak level.
        n_inputs = len(list(self.data.values())[0])
        self.steps_per_epoch = (n_inputs - 1) // batch_size + 1
        if total_steps < 0:
            total_steps = -total_steps * self.steps_per_epoch
        self.total_steps = int(total_steps)
        if not target_steps:
            target_steps = self.total_steps
        elif target_steps < 0:
            target_steps = - target_steps * self.steps_per_epoch
        target_steps = int(target_steps)
        if target_steps > self.total_steps:
            raise ValueError('Target steps can\'t exceed total steps.')
        self.num_warmup_steps = int(self.total_steps * warmup_ratio)

        # Define optimization process, build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            self._global_step = optimization.get_global_step()
            self._optimizer = optimization.get_optimizer(
                init_lr=learning_rate,
                global_step=self._global_step,
                num_train_steps=self.total_steps,
                num_warmup_steps=self.num_warmup_steps,
                key_to_depths=self._key_to_depths,
                **kwargs)
            return self._build('fit', **kwargs).run(
                target_steps,
                print_per_secs=print_per_secs,
                save_per_steps=save_per_steps)

    def predict(self, X=None, X_tokenized=None, batch_size=8):
        ''' Inference on the model.

        Args:
            X: list. A list object consisting untokenized inputs.
            X_tokenized: list. A list object consisting tokenized inputs.
              Either `X` or `X_tokenized` should be None.
            batch_size: int. The size of batch in each step.
        Returns:
            A dict object of model outputs.
        '''
        # NOTE: This method is reimplemented by `FastBERTClassifier`,
        # `GPT2LM`, `BERTNER`.

        # Make sure the arguments are correct.
        self.batch_size = batch_size
        if self._gpu_ids:
            assert batch_size % len(self._gpu_ids) == 0, (
                '`batch_size` should be evenly divided by the number of GPUs, '
                'but got %d and %d.'
                % (batch_size, len(self._gpu_ids)))

        # Make sure necessary arguments are on spot.
        if not self._graph_built:
            _attr_dict = self.__class__._INFER_ATTRIBUTES
            _miss_dict = set()
            for attr in _attr_dict:
                if self.__getattribute__(attr) is None:
                    _miss_dict.add(attr)
            if _miss_dict:
                _miss_info = []
                for attr in _miss_dict:
                    _miss_info += ['`%s`: %s' % (attr, _attr_dict[attr])]
                raise ValueError(
                    'Intialize or train the model first, or feed value for '
                    'the following necessary arguments (%s), before running '
                    'inference.'
                    % '; '.join(_miss_info))

        # Convert raw data to structed data. This method
        # should be specifically implemented by child classes.
        self.data = self._parallel_convert(
            X, None, None, X_tokenized, is_training=False)

        # Build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            return self._build('predict').run()

    def score(self, X=None, y=None, sample_weight=None, X_tokenized=None,
              batch_size=8):
        ''' Inference on the model with scoring.

        Args:
            X: list. A list object consisting untokenized inputs.
            y: list. A list object consisting labels.
            sample_weight: list. A list object of float-convertable values.
            X_tokenized: list. A list object consisting tokenized inputs.
              Either `X` or `X_tokenized` should be None.
            batch_size: int. The size of batch in each step.
        Returns:
            A dict object of output metrics.
        '''
        # NOTE: This method is reimplemented by `FastBERTClassifier` and
        # `GPT2LM`.
        assert y is not None, '`y` can\'t be None.'

        # Make sure the arguments are correct.
        self.batch_size = batch_size
        if self._gpu_ids:
            assert batch_size % len(self._gpu_ids) == 0, (
                '`batch_size` should be evenly divided by the number of GPUs, '
                'but got %d and %d.'
                % (batch_size, len(self._gpu_ids)))

        # Make sure necessary arguments are on spot.
        if not self._graph_built:
            _attr_dict = self.__class__._INFER_ATTRIBUTES
            _miss_dict = set()
            for attr in _attr_dict:
                if self.__getattribute__(attr) is None:
                    _miss_dict.add(attr)
            if _miss_dict:
                _miss_info = []
                for attr in _miss_dict:
                    _miss_info += ['`%s`: %s' % (attr, _attr_dict[attr])]
                raise ValueError(
                    'Intialize or train the model first, or feed value for '
                    'the following necessary arguments (%s), before running '
                    'inference.'
                    % '; '.join(_miss_info))

        # Convert raw data to structed data. This method
        # should be specifically implemented by child classes.
        self.data = self._parallel_convert(
            X, y, sample_weight, X_tokenized, is_training=False)

        # Build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            return self._build('score').run()

    def save(self, max_to_keep=10000):
        ''' Save model into checkpoint file.

        When attribute `output_dir` is None, the method is illegal. Otherwise
        the model will be saved into `"model.checkpoint-%s" % step` under
        the directory of `output_dir`.

        Args:
            max_to_keep: int. Max number of checkpoints to save.
        '''
        if not self._graph_built:
            raise ValueError(
                'Randomly initialize, fit, predict or score before saving '
                'checkpoint.')

        if not self.output_dir:
            raise ValueError('Attribute `output_dir` is None.')

        tf.logging.info(
            'Saving checkpoint for %d into %s/model.ckpt'
            % (self.step, self.output_dir))
        self.init_checkpoint = (
            self.output_dir + '/model.ckpt-%d' % self.step)

        with self.graph.as_default():
            saver = tf.train.Saver(max_to_keep=max_to_keep)
            saver.save(self.sess, self.init_checkpoint)

    def cache(self, code, cache_file='./.cache', max_to_keep=10000, note=''):
        ''' Save model configurations into cache file.

        Args:
            code: string. Unique name of configuration to save. Can be any
              kind of string.
            cache_file: string. The path of cache file.
            max_to_keep: int. Max number of checkpoints to save.
            note: string. The information you with to note.
        Returns:
            None

        When attribute `output_dir` is not None, the method will save the
        model into checkpoint file simultaneously.
        '''
        if self.output_dir and self._graph_built:
            self.save(max_to_keep)
        tf.logging.info('Saving model configuration `%s` into %s'
                        % (code, cache_file))

        if os.path.exists(cache_file):
            cache_fp = open(cache_file, encoding='utf-8')
            cache_json = json.load(cache_fp)
            cache_fp.close()
        else:
            cache_json = {}

        _cache_json = {
            'model': self.__class__.__name__,
            '__init__': {}}
        if note:
            _cache_json['note'] = note

        for key in self.__class__.__init__.__code__.co_varnames[1:]:
            try:
                value = self.__getattribute__(key)
            except:
                value = self.__init_args__[key]

            # convert to relative path
            if key == 'init_checkpoint' or key.endswith('_dir') or \
                    key.endswith('_file'):
                if isinstance(value, str) and not value.startswith('/'):
                    value = utils.get_relative_path(
                        source=cache_file,
                        target=value)

            _cache_json['__init__'][key] = value
        cache_json[code] = _cache_json

        cache_fp = open(cache_file, 'w', encoding='utf-8')
        json.dump(cache_json, cache_fp, indent=2)
        cache_fp.close()

    def init(self, reinit_all=False):
        ''' Initialize the graph randomly or from checkpoint file.

        Args:
            reinit_all: bool. Set to True if you wish to re-initialize the
              graph with random values.
        '''

        # Make sure necessary arguments are on spot.
        if not self._graph_built:
            _attr_dict = self.__class__._INFER_ATTRIBUTES
            _miss_dict = set()
            for attr in _attr_dict:
                if attr == 'init_checkpoint':
                    continue
                if self.__getattribute__(attr) is None:
                    _miss_dict.add(attr)
            if _miss_dict:
                _miss_info = []
                for attr in _miss_dict:
                    _miss_info += ['`%s`: %s' % (attr, _attr_dict[attr])]
                raise ValueError(
                    'Feed value for the following necessary arguments '
                    'before initialization. (%s)' % '; '.join(_miss_info))

        # Build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            return self._build('init').run(
                reinit_all=reinit_all,
                ignore_checkpoint=(self.init_checkpoint is None))

    def reinit_from_checkpoint(self, init_checkpoint=None,
                               assignment_map=None):
        ''' Reinitialize variables from checkpoint file.

        Args:
            init_checkpoint: string. Path of checkpoint file from which to
              load. If set to None, use `init_checkpoint` of the module.
            assignment_map: dict. A dict object that maps from variable name
              in checkpoint to variables in local graph. If set to None, use
              `assignment_map` of the module.
        '''

        if not init_checkpoint:
            if not self.init_checkpoint:
                raise ValueError('No checkpoint file assigned for the module.')
            init_checkpoint = self.init_checkpoint
        checkpoint_path = utils.get_checkpoint_path(init_checkpoint)
        if not checkpoint_path:
            raise ValueError('Checkpoint file \'%s\' does not exist. '
                             'Make sure you pass correct value to '
                             '`init_checkpoint`.' % init_checkpoint)
        self.init_checkpoint = checkpoint_path

        continual = os.path.dirname(checkpoint_path) == self.output_dir
        if continual:
            self.step = int(checkpoint_path.split('-')[-1])

        # Add new global variables into assignment_map
        if 'assignment_map' not in self.__dict__:
            self.assignment_map = {}
        if not assignment_map:
            (assignment_map, _) = utils.get_assignment_map(
                checkpoint_path, self.global_variables, continual=False)
            for key in assignment_map:
                if key not in self.assignment_map:
                    self.assignment_map[key] = assignment_map[key]
        else:
            self.assignment_map = assignment_map

        with self.graph.as_default():
            loader = tf.train.Saver(self.assignment_map)
            loader.restore(self.sess, checkpoint_path)
        try:
            self.sess.run(tf.assign(self._global_step, self.step))
        except AttributeError:
            pass

        new_uninited_vars = {}
        for var in self.global_variables:
            if var not in self.assignment_map.values():
                new_uninited_vars[var.name[:-2]] = var
        self.uninited_vars = new_uninited_vars

    @property
    def trainable_variables(self):
        return self.graph._collections.get('trainable_variables', [])

    @property
    def global_variables(self):
        return self.graph._collections.get('variables', [])

    def export(self, export_dir, rename_inputs=None, rename_outputs=None,
               ignore_outputs=None):
        ''' Export model into SavedModel files.

        Args:
            export_dir: str. Directory to which the model is saved.
            rename_inputs: dict. Mapping of original name to target name.
            rename_outputs: dict. Mapping of original name to target name.
            ignore_outputs: list. Name of outputs to ignore.
        Returns:
            None
        '''
        # NOTE: This method is reimplemented by `FastBERTClassifier` and
        # `GPT2LM`.
        tf.gfile.MakeDirs(export_dir)

        # Make sure necessary arguments are on spot.
        if not self._graph_built:
            _attr_dict = self.__class__._INFER_ATTRIBUTES
            _miss_dict = set()
            for attr in _attr_dict:
                if self.__getattribute__(attr) is None:
                    _miss_dict.add(attr)
            if _miss_dict:
                _miss_info = []
                for attr in _miss_dict:
                    _miss_info += ['`%s`: %s' % (attr, _attr_dict[attr])]
                raise ValueError(
                    'Feed value for the following necessary arguments '
                    'before exportation of PB files. (%s)'
                    % '; '.join(_miss_info))

        # Build the graph, and then run.
        with self.graph.as_default(), \
                tf.variable_scope('', reuse=tf.AUTO_REUSE):
            self._build('export').run(
                export_dir, rename_inputs, rename_outputs, ignore_outputs)

    def _parallel_convert(self, X=None, y=None, sample_weight=None,
                          X_tokenized=None, is_training=False):
        ''' Parallel data conversion in multi processes, a general method. '''

        if utils.NUM_PROCESSES <= 1:
            return self.convert(X, y, sample_weight, X_tokenized, is_training)

        tf.logging.info('Parsing input data on %d parallel processes'
                        % utils.NUM_PROCESSES)

        n_inputs = len(X if X else X_tokenized)
        n_buckets = max(min(n_inputs, utils.NUM_PROCESSES), 1)

        buckets = [{'X': [] if X else None,
                    'y': [] if y else None,
                    'sample_weight': [] if sample_weight else None,
                    'X_tokenized': [] if X_tokenized else None}
                   for _ in range(n_buckets)]
        for i in range(n_inputs):
            index = i % n_buckets
            if X:
                buckets[index]['X'].append(X[i])
            if y:
                buckets[index]['y'].append(y[i])
            if sample_weight:
                buckets[index]['sample_weight'].append(sample_weight[i])
            if X_tokenized:
                buckets[index]['X_tokenized'].append(X_tokenized[i])

        values = utils.get_init_values(self)
        args = zip(list(range(n_buckets)),
                   [self.__class__ for _ in range(n_buckets)],
                   [values for _ in range(n_buckets)],
                   buckets,
                   [is_training for _ in range(n_buckets)])
        data_buckets = utils.pool.map(utils._parallel_convert_single_process, args)

        data = {}
        data_buckets.sort(key=lambda x: x[0])    # re-order inputs
        keys = list(data_buckets[0][1].keys())
        for key in keys:
            data[key] = utils.transform(
                [_data[1][key] for _data in data_buckets])
        return data

    @abstractmethod
    def convert(self, *args, **kwargs):
        raise NotImplementedError()

    @staticmethod
    def _convert_sample_weight(sample_weight, n_inputs):
        ''' Standardize `sample_weight`. '''
        if sample_weight:
            try:
                return [float(item) for item in sample_weight]
            except ValueError:
                raise ValueError(
                    '`sample_weight` must be a list of float-'
                    'convertable values.')
        return [1.0 for _ in range(n_inputs)]

    @staticmethod
    def _assert_legal(X, y, sample_weight, X_tokenized):
        ''' Make sure strange errors intrigged by data not occur. '''

        if X:
            if X_tokenized:
                raise ValueError('Set None to one of `X` and `X_tokenized`.')
            assert isinstance(X, list), '`X` should be a list object.'
        else:
            if not X_tokenized:
                raise ValueError('Must pass value to `X` or `X_tokenized`.')
            assert isinstance(X_tokenized, list), (
                '`X_tokenized` should be a list object.')
            X = X_tokenized

        if y:
            assert isinstance(y, list), '`y` should be a list object.'
            assert len(X) == len(y), (
                'Length of `y` should be the same with `X/X_tokenized`. '
                '(%d vs. %d)' % (len(y), len(X)))

        if sample_weight:
            assert isinstance(sample_weight, list), (
                '`sample_weight` should be a list object.')
            assert len(X) == len(sample_weight), (
                'Length of `sample_weight` should be the '
                'same with `X/X_tokenized`. (%d vs. %d)' % (len(y), len(X)))

    def _build(self, work, **kwargs):
        ''' Build the computation graph. '''

        # Build work flow with computation graph. Multi-GPU
        # training and inference are supported. Temporarily
        # not support running on TPUs.
        if work == 'fit':
            if 'EMD' in self.__class__.__name__:
                return processing.EMDTraining(self, **kwargs)
            if kwargs.get('adversarial'):
                return processing.AdversarialTraining(self, **kwargs)
            return processing.BasicTraining(self, **kwargs)
        elif work == 'predict':
            return processing.BasicInference(self, **kwargs)
        elif work == 'score':
            return processing.BasicScoring(self, **kwargs)
        elif work == 'export':
            return processing.ExportInference(self, **kwargs)
        elif work == 'init':
            return processing.Initialization(self, **kwargs)

    def assign(self, variable, value):
        ''' Manually assign values for a parameter. '''
        assign_op = tf.assign(variable, value)
        self.sess.run(assign_op)

    @abstractmethod
    def _set_placeholders(self, *args, **kwargs):
        raise NotImplementedError()

    def _parallel_forward(self, is_training=True, **kwargs):
        ''' Parallel foundation of computation graph in multi GPUs,
        a general method. '''

        # We implement data parallelization instead of model
        # parallelization, for this design suits most real cases.
        all_grads = []
        all_losses = []
        all_probs = []
        all_preds = []
        n_device = len(self._gpu_ids) if self._gpu_ids else 1
        split_placeholders = {key: {} for key in range(n_device)}
        for name, placeholder in self.placeholders.items():
            split_placeholder = tf.split(placeholder, n_device, axis=0)
            for key in range(n_device):
                split_placeholders[key][name] = split_placeholder[key]

        # map
        # The `Null` class makes the following codes about running on GPUs
        # compatible with running on CPU.
        device = utils.Null if n_device <= 1 else tf.device
        for idx in range(n_device):
            _gpu_id = self._gpu_ids[idx] if self._gpu_ids else ''
            with device('gpu:%s' % _gpu_id):
                (total_loss, d_losses, d_probs, d_preds) = self._forward(
                    is_training=is_training,
                    split_placeholders=split_placeholders[idx],
                    **kwargs)

                if is_training:
                    # This is the so-called 'backward' process
                    d_grads = tf.gradients(
                        total_loss, self.trainable_variables)
                    all_grads.append(d_grads)

                all_losses.append(d_losses)
                all_probs.append(d_probs)
                all_preds.append(d_preds)

        # reduce
        losses = collections.OrderedDict()
        probs = collections.OrderedDict()
        preds = collections.OrderedDict()
        for key in d_losses:
            _losses = [d_losses[key] for d_losses in all_losses]
            losses[key] = tf.concat(_losses, axis=0)
        for key in d_probs:
            _probs = [d_probs[key] for d_probs in all_probs]
            probs[key] = tf.concat(_probs, axis=0)
        for key in d_preds:
            _preds = [d_preds[key] for d_preds in all_preds]
            preds[key] = tf.concat(_preds, axis=0)

        # average, clip, and apply gradients
        grads = None
        if is_training:

            # average gradients
            # This process can be generalized to one device, so we do not
            # add another `if` expression.
            average_grads = []
            for i in range(len(self.trainable_variables)):
                split_grads = []
                for d_grads in all_grads:
                    if d_grads[i] is not None:
                        split_grads.append(d_grads[i])
                if split_grads:
                    average_grad = utils.average_n_grads(split_grads)
                    average_grads.append(average_grad)
                else:
                    average_grads.append(None)

            # clip gradients
            (grads, _) = tf.clip_by_global_norm(average_grads, clip_norm=1.0)

        return (grads, losses, probs, preds)

    @abstractmethod
    def _forward(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_fit_ops(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_fit_info(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_predict_ops(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_predict_outputs(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_score_ops(self, *args, **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def _get_score_outputs(self, *args, **kwargs):
        raise NotImplementedError()
