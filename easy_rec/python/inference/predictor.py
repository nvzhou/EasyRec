# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import logging
import math
import os
import time

import numpy as np
import six
import tensorflow as tf
from tensorflow.core.protobuf import meta_graph_pb2
from tensorflow.python.platform import gfile
from tensorflow.python.saved_model import constants
from tensorflow.python.saved_model import signature_constants

from easy_rec.python.utils import pai_util
from easy_rec.python.utils.config_util import get_configs_from_pipeline_file
from easy_rec.python.utils.input_utils import get_type_defaults
from easy_rec.python.utils.load_class import get_register_class_meta

if tf.__version__ >= '2.0':
  tf = tf.compat.v1

SINGLE_PLACEHOLDER_FEATURE_KEY = 'features'

_PREDICTOR_CLASS_MAP = {}
_register_abc_meta = get_register_class_meta(
    _PREDICTOR_CLASS_MAP, have_abstract_class=True)


class PredictorInterface(six.with_metaclass(_register_abc_meta, object)):
  version = 1

  def __init__(self, model_path, model_config=None):
    """Init tensorflow session and load tf model.

    Args:
      model_path:  init model from this directory
      model_config: config string for model to init, in json format
    """
    pass

  @abc.abstractmethod
  def predict(self, input_data, batch_size):
    """Using session run predict a number of samples using batch_size.

    Args:
      input_data:  a list of numpy array, each array is a sample to be predicted
      batch_size: batch_size passed by the caller, you can also ignore this param and
        use a fixed number if you do not want to adjust batch_size in runtime

    Returns:
      result: a list of dict, each dict is the prediction result of one sample
        eg, {"output1": value1, "output2": value2}, the value type can be
        python int str float, and numpy array
    """
    pass

  def get_output_type(self):
    """Get output types of prediction.

    In this function user should return a type dict, which indicates which type of
    data should the output of predictor be converted to.

    In this function user should return a type dict, which indicates
    which type of data should the output of predictor be converted to
    * type json, data will be serialized to json str

    * type image, data will be converted to encode image binary and write to oss file,
      whose name is output_dir/${key}/${input_filename}_${idx}.jpg, where input_filename
      is extracted from url, key corresponds to the key in the dict of output_type,
      if the type of data indexed by key is a list, idx is the index of element in list, otherwhile ${idx} will be empty

    * type video, data will be converted to encode video binary and write to oss file,

    eg:  return  {
      'image': 'image',
      'feature': 'json'
    }

    indicating that the image data in the output dict will be save to image
    file and feature in output dict will be converted to json
    """
    return {}


class PredictorImpl(object):

  def __init__(self, model_path, profiling_file=None):
    """Impl class for predictor.

    Args:
      model_path:  saved_model directory or frozenpb file path
      profiling_file:  profiling result file, default None.
        if not None, predict function will use Timeline to profiling
        prediction time, and the result json will be saved to profiling_file
    """
    self._inputs_map = {}
    self._outputs_map = {}
    self._is_saved_model = False
    self._profiling_file = profiling_file
    self._model_path = model_path
    self._input_names = []
    self._is_multi_placeholder = True

    self._build_model()

  @property
  def input_names(self):
    return self._input_names

  @property
  def output_names(self):
    return list(self._outputs_map.keys())

  def __del__(self):
    """Destroy predictor resources."""
    self._session.close()

  def search_pb(self, directory):
    """Search pb file recursively in model directory. if multiple pb files exist, exception will be raised.

    If multiple pb files exist, exception will be raised.

    Args:
      directory: model directory.

    Returns:
      directory contain pb file
    """
    dir_list = []
    for root, dirs, files in gfile.Walk(directory):
      for f in files:
        _, ext = os.path.splitext(f)
        if ext == '.pb':
          dir_list.append(root)
    if len(dir_list) == 0:
      raise ValueError('savedmodel is not found in directory %s' % directory)
    elif len(dir_list) > 1:
      raise ValueError('multiple saved model found in directory %s' % directory)

    return dir_list[0]

  def _get_input_fields_from_pipeline_config(self, model_path):
    pipeline_path = os.path.join(model_path, 'assets/pipeline.config')
    assert gfile.Exists(pipeline_path), '%s not exists.' % pipeline_path
    pipeline_config = get_configs_from_pipeline_file(pipeline_path)
    input_fields = pipeline_config.data_config.input_fields
    input_fields_info = {
        input_field.input_name:
        (input_field.input_type, input_field.default_val)
        for input_field in input_fields
    }
    input_fields_list = [input_field.input_name for input_field in input_fields]

    return input_fields_info, input_fields_list

  def _build_model(self):
    """Load graph from model_path and create session for this graph."""
    model_path = self._model_path
    self._graph = tf.Graph()
    gpu_options = tf.GPUOptions(allow_growth=True)
    session_config = tf.ConfigProto(
        gpu_options=gpu_options,
        allow_soft_placement=True,
        log_device_placement=(self._profiling_file is not None))
    self._session = tf.Session(config=session_config, graph=self._graph)

    with self._graph.as_default():
      with self._session.as_default():
        # load model
        _, ext = os.path.splitext(model_path)
        tf.logging.info('loading model from %s' % model_path)
        if gfile.IsDirectory(model_path):
          model_path = self.search_pb(model_path)
          logging.info('model find in %s' % model_path)
          self._input_fields_info, self._input_fields_list = self._get_input_fields_from_pipeline_config(
              model_path)
          assert tf.saved_model.loader.maybe_saved_model_directory(model_path), \
              'saved model does not exists in %s' % model_path
          self._is_saved_model = True
          meta_graph_def = tf.saved_model.loader.load(
              self._session, [tf.saved_model.tag_constants.SERVING], model_path)
          # parse signature
          signature_def = meta_graph_def.signature_def[
              signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY]
          inputs = signature_def.inputs
          # each input_info is a tuple of input_id, name, data_type
          input_info = []
          self._is_multi_placeholder = len(inputs.items()) > 1
          if self._is_multi_placeholder:
            for gid, item in enumerate(inputs.items()):
              name, tensor = item
              logging.info('Load input binding: %s -> %s' % (name, tensor.name))
              input_name = tensor.name
              input_name, _ = input_name.split(':')
              try:
                input_id = input_name.split('_')[-1]
                input_id = int(input_id)
              except Exception:
                # support for models that are not exported by easy_rec
                # in which case, the order of inputs may not be the
                # same as they are defined, thereforce, list input
                # could not be supported, only dict input could be supported
                logging.warning(
                    'could not determine input_id from input_name: %s' %
                    input_name)
                input_id = gid
              input_info.append((input_id, name, tensor.dtype))
              self._inputs_map[name] = self._graph.get_tensor_by_name(
                  tensor.name)
          else:
            # only one input, all features concatenate together
            for name, tensor in inputs.items():
              logging.info('Load input binding: %s -> %s' % (name, tensor.name))
              input_info.append((0, name, tensor.dtype))
              self._inputs_map[name] = self._graph.get_tensor_by_name(
                  tensor.name)
          # sort inputs by input_ids so as to match the order of csv data
          input_info.sort(key=lambda t: t[0])
          self._input_names = [t[1] for t in input_info]

          outputs = signature_def.outputs
          for name, tensor in outputs.items():
            logging.info('Load output binding: %s -> %s' % (name, tensor.name))
            self._outputs_map[name] = self._graph.get_tensor_by_name(
                tensor.name)

          # get assets
          self._assets = {}
          asset_files = tf.get_collection(constants.ASSETS_KEY)
          for any_proto in asset_files:
            asset_file = meta_graph_pb2.AssetFileDef()
            any_proto.Unpack(asset_file)
            type_name = asset_file.tensor_info.name.split(':')[0]
            asset_path = os.path.join(model_path, constants.ASSETS_DIRECTORY,
                                      asset_file.filename)
            assert gfile.Exists(
                asset_path), '%s is missing in saved model' % asset_path
            self._assets[type_name] = asset_path
          logging.info(self._assets)

          # get export config
          self._export_config = {}
          # export_config_collection = tf.get_collection(fields.EVGraphKeys.export_config)
          # if len(export_config_collection) > 0:
          #  self._export_config = json.loads(export_config_collection[0])
          #  logging.info('load export config info %s' % export_config_collection[0])
        else:
          raise ValueError('currently only savedmodel is supported')

  def predict(self, input_data_dict, output_names=None):
    """Predict input data with loaded model.

    Args:
      input_data_dict: a dict containing all input data, key is the input name,
        value is the corresponding value
      output_names:  if not None, will fetch certain outputs, if set None, will
        return all the output info according to the output info in model signature

    Return:
      a dict of outputs, key is the output name, value is the corresponding value
    """
    feed_dict = {}
    for input_name, tensor in six.iteritems(self._inputs_map):
      assert input_name in input_data_dict, 'input data %s is missing' % input_name
      tensor_shape = tensor.get_shape().as_list()
      input_shape = input_data_dict[input_name].shape
      assert tensor_shape[0] is None or (tensor_shape[0] == input_shape[0]), \
          'input %s  batchsize %d is not the same as the exported batch_size %d' % \
          (input_name, input_shape[0], tensor_shape[0])
      feed_dict[tensor] = input_data_dict[input_name]
    fetch_dict = {}
    if output_names is not None:
      for output_name in output_names:
        assert output_name in self._outputs_map, \
            'invalid output name %s' % output_name
        fetch_dict[output_name] = self._outputs_map[output_name]
    else:
      fetch_dict = self._outputs_map

    with self._graph.as_default():
      with self._session.as_default():
        if self._profiling_file is None:
          return self._session.run(fetch_dict, feed_dict)
        else:
          run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
          run_metadata = tf.RunMetadata()
          results = self._session.run(
              fetch_dict,
              feed_dict,
              options=run_options,
              run_metadata=run_metadata)
          # Create the Timeline object, and write it to a json
          from tensorflow.python.client import timeline
          tl = timeline.Timeline(run_metadata.step_stats)
          ctf = tl.generate_chrome_trace_format()
          with gfile.GFile(self._profiling_file, 'w') as f:
            f.write(ctf)
          return results


class Predictor(PredictorInterface):

  def __init__(self, model_path, profiling_file=None):
    """Initialize a `Predictor`.

    Args:
      model_path:  saved_model directory or frozenpb file path
      profiling_file:  profiling result file, default None.
        if not None, predict function will use Timeline to profiling
        prediction time, and the result json will be saved to profiling_file
    """
    self._predictor_impl = PredictorImpl(model_path, profiling_file)
    self._inputs_map = self._predictor_impl._inputs_map
    self._outputs_map = self._predictor_impl._outputs_map
    self._profiling_file = profiling_file
    self._export_config = self._predictor_impl._export_config
    self._input_fields_info = self._predictor_impl._input_fields_info
    self._is_multi_placeholder = self._predictor_impl._is_multi_placeholder

    self._input_fields = self._predictor_impl._input_fields_list

  @property
  def input_names(self):
    """Input names of the model.

    Returns:
      a list, which conaining the name of input nodes available in model
    """
    return list(self._inputs_map.keys())

  @property
  def output_names(self):
    """Output names of the model.

    Returns:
      a list, which conaining the name of outputs nodes available in model
    """
    return list(self._outputs_map.keys())

  def predict_impl(self,
                   input_table,
                   output_table,
                   all_cols='',
                   all_col_types='',
                   selected_cols='',
                   reserved_cols='',
                   output_cols=None,
                   batch_size=1024,
                   slice_id=0,
                   slice_num=1,
                   input_sep=',',
                   output_sep=chr(1)):
    """Predict table input with loaded model.

    Args:
      input_table: table/file_path to read
      output_table: table/file_path to write
      all_cols: union of columns
      all_col_types: data types of the columns
      selected_cols: included column names, comma separated, such as "a,b,c"
      reserved_cols: columns to be copy to output_table, comma separated, such as "a,b"
      output_cols: output columns, comma separated, such as "y float, embedding string",
                the output names[y, embedding] must be in saved_model output_names
      batch_size: predict batch size
      slice_id: when multiple workers write the same table, each worker should
                be assigned different slice_id, which is usually slice_id
      slice_num: table slice number
      input_sep: separator of input file.
      output_sep: separator of predict result file.
    """
    if pai_util.is_on_pai():
      self.predict_table(
          input_table,
          output_table,
          all_cols=all_cols,
          all_col_types=all_col_types,
          selected_cols=selected_cols,
          reserved_cols=reserved_cols,
          output_cols=output_cols,
          batch_size=batch_size,
          slice_id=slice_id,
          slice_num=slice_num)
    else:
      self.predict_csv(
          input_table,
          output_table,
          reserved_cols=reserved_cols,
          output_cols=output_cols,
          batch_size=batch_size,
          slice_id=slice_id,
          slice_num=slice_num,
          input_sep=input_sep,
          output_sep=output_sep)

  def predict_csv(self, input_path, output_path, reserved_cols, output_cols,
                  batch_size, slice_id, slice_num, input_sep, output_sep):
    record_defaults = [
        self._input_fields_info[col_name][1] for col_name in self._input_fields
    ]
    if reserved_cols == 'ALL_COLUMNS':
      reserved_cols = self._input_fields
    else:
      reserved_cols = [x.strip() for x in reserved_cols.split(',') if x != '']
    if output_cols is None or output_cols == 'ALL_COLUMNS':
      output_cols = sorted(self._predictor_impl.output_names)
      logging.info('predict output cols: %s' % output_cols)
    else:
      # specified as score float,embedding string
      tmp_cols = []
      for x in output_cols.split(','):
        if x.strip() == '':
          continue
        tmp_keys = x.split(' ')
        tmp_cols.append(tmp_keys[0].strip())
      output_cols = tmp_cols

    with tf.Graph().as_default(), tf.Session() as sess:
      num_parallel_calls = 8
      file_paths = []
      for x in input_path.split(','):
        file_paths.extend(gfile.Glob(x))
      assert len(file_paths) > 0, 'match no files with %s' % input_path

      dataset = tf.data.Dataset.from_tensor_slices(file_paths)
      parallel_num = min(num_parallel_calls, len(file_paths))
      dataset = dataset.interleave(
          tf.data.TextLineDataset,
          cycle_length=parallel_num,
          num_parallel_calls=parallel_num)
      dataset = dataset.shard(slice_num, slice_id)
      logging.info('batch_size = %d' % batch_size)
      dataset = dataset.batch(batch_size)
      dataset = dataset.prefetch(buffer_size=64)

      def _parse_csv(line):

        def _check_data(line):
          sep = input_sep
          if type(sep) != type(str):
            sep = sep.encode('utf-8')
          field_num = len(line[0].split(sep))
          assert field_num == len(record_defaults), 'sep[%s] maybe invalid: field_num=%d, required_num=%d' \
                                                    % (sep, field_num, len(record_defaults))
          return True

        check_op = tf.py_func(_check_data, [line], Tout=tf.bool)
        with tf.control_dependencies([check_op]):
          fields = tf.decode_csv(
              line,
              field_delim=',',
              record_defaults=record_defaults,
              name='decode_csv')

        inputs = {self._input_fields[x]: fields[x] for x in range(len(fields))}
        return inputs

      dataset = dataset.map(_parse_csv, num_parallel_calls=num_parallel_calls)
      iterator = dataset.make_one_shot_iterator()
      all_dict = iterator.get_next()

      if not gfile.Exists(output_path):
        gfile.MakeDirs(output_path)
      res_path = os.path.join(output_path, 'slice_%d.csv' % slice_id)
      table_writer = gfile.GFile(res_path, 'w')

      input_names = self._predictor_impl.input_names
      progress = 0
      sum_t0, sum_t1, sum_t2 = 0, 0, 0
      pred_cnt = 0
      table_writer.write(output_sep.join(output_cols + reserved_cols) + '\n')
      while True:
        try:
          ts0 = time.time()
          all_vals = sess.run(all_dict)

          ts1 = time.time()
          input_vals = {k: all_vals[k] for k in input_names}
          outputs = self._predictor_impl.predict(input_vals, output_cols)

          for x in output_cols:
            if outputs[x].dtype == np.object:
              outputs[x] = [val.decode('utf-8') for val in outputs[x]]
          for k in reserved_cols:
            if all_vals[k].dtype == np.object:
              all_vals[k] = [val.decode('utf-8') for val in all_vals[k]]

          ts2 = time.time()
          reserve_vals = [outputs[x] for x in output_cols] + \
                         [all_vals[k] for k in reserved_cols]
          outputs = [x for x in zip(*reserve_vals)]
          pred_cnt += len(outputs)
          outputs = '\n'.join(
              [output_sep.join([str(i) for i in output]) for output in outputs])
          table_writer.write(outputs + '\n')

          ts3 = time.time()
          progress += 1
          sum_t0 += (ts1 - ts0)
          sum_t1 += (ts2 - ts1)
          sum_t2 += (ts3 - ts2)
        except tf.errors.OutOfRangeError:
          break
        if progress % 100 == 0:
          logging.info('progress: batch_num=%d sample_num=%d' %
                       (progress, progress * batch_size))
          logging.info('time_stats: read: %.2f predict: %.2f write: %.2f' %
                       (sum_t0, sum_t1, sum_t2))
      logging.info('Final_time_stats: read: %.2f predict: %.2f write: %.2f' %
                   (sum_t0, sum_t1, sum_t2))
      table_writer.close()
      logging.info('Predict %s done.' % input_path)
      logging.info('Predict size: %d.' % pred_cnt)

  def predict_table(self,
                    input_table,
                    output_table,
                    all_cols,
                    all_col_types,
                    selected_cols,
                    reserved_cols,
                    output_cols=None,
                    batch_size=1024,
                    slice_id=0,
                    slice_num=1):

    def _get_defaults(col_name, col_type):
      if col_name in self._input_fields_info:
        col_type, default_val = self._input_fields_info[col_name]
        default_val = get_type_defaults(col_type, default_val)
        logging.info('col_name: %s, default_val: %s' % (col_name, default_val))
      else:
        logging.info('col_name: %s is not used in predict.' % col_name)
        defaults = {'string': '', 'double': 0.0, 'bigint': 0}
        assert col_type in defaults, 'invalid col_type: %s, col_type: %s' % (
            col_name, col_type)
        default_val = defaults[col_type]
      return default_val

    all_cols = [x.strip() for x in all_cols.split(',') if x != '']
    all_col_types = [x.strip() for x in all_col_types.split(',') if x != '']
    reserved_cols = [x.strip() for x in reserved_cols.split(',') if x != '']

    if output_cols is None:
      output_cols = self._predictor_impl.output_names
    else:
      # specified as score float,embedding string
      tmp_cols = []
      for x in output_cols.split(','):
        if x.strip() == '':
          continue
        tmp_keys = x.split(' ')
        tmp_cols.append(tmp_keys[0].strip())
      output_cols = tmp_cols

    record_defaults = [
        _get_defaults(col_name, col_type)
        for col_name, col_type in zip(all_cols, all_col_types)
    ]
    with tf.Graph().as_default(), tf.Session() as sess:
      num_parallel_calls = 8
      input_table = input_table.split(',')
      dataset = tf.data.TableRecordDataset([input_table],
                                           record_defaults=record_defaults,
                                           slice_id=slice_id,
                                           slice_count=slice_num,
                                           selected_cols=','.join(all_cols))

      logging.info('batch_size = %d' % batch_size)
      dataset = dataset.batch(batch_size)
      dataset = dataset.prefetch(buffer_size=64)

      def _parse_table(*fields):
        fields = list(fields)
        field_dict = {all_cols[i]: fields[i] for i in range(len(fields))}
        return field_dict

      dataset = dataset.map(_parse_table, num_parallel_calls=num_parallel_calls)
      iterator = dataset.make_one_shot_iterator()
      all_dict = iterator.get_next()

      import common_io
      table_writer = common_io.table.TableWriter(
          output_table, slice_id=slice_id)

      input_names = self._predictor_impl.input_names

      def _parse_value(all_vals):
        if self._is_multi_placeholder:
          if SINGLE_PLACEHOLDER_FEATURE_KEY in all_vals:
            feature_vals = all_vals[SINGLE_PLACEHOLDER_FEATURE_KEY]
            split_index = []
            split_vals = {}
            for i, k in enumerate(input_names):
              split_index.append(k)
              split_vals[k] = []
            for record in feature_vals:
              split_records = record.split('\002')
              for i, r in enumerate(split_records):
                split_vals[split_index[i]].append(r)
            return {k: np.array(split_vals[k]) for k in input_names}
        return {k: all_vals[k] for k in input_names}

      progress = 0
      sum_t0, sum_t1, sum_t2 = 0, 0, 0

      while True:
        try:
          ts0 = time.time()
          all_vals = sess.run(all_dict)

          ts1 = time.time()
          input_vals = _parse_value(all_vals)
          # logging.info('input names = %s' % input_names)
          # logging.info('input vals = %s' % input_vals)
          outputs = self._predictor_impl.predict(input_vals, output_cols)

          ts2 = time.time()
          reserve_vals = [all_vals[k] for k in reserved_cols
                          ] + [outputs[x] for x in output_cols]
          indices = list(range(0, len(reserve_vals)))
          outputs = [x for x in zip(*reserve_vals)]

          table_writer.write(outputs, indices, allow_type_cast=False)
          ts3 = time.time()
          progress += 1
          sum_t0 += (ts1 - ts0)
          sum_t1 += (ts2 - ts1)
          sum_t2 += (ts3 - ts2)
        except tf.python_io.OutOfRangeException:
          break
        except tf.errors.OutOfRangeError:
          break
        if progress % 100 == 0:
          logging.info('progress: batch_num=%d sample_num=%d' %
                       (progress, progress * batch_size))
          logging.info('time_stats: read: %.2f predict: %.2f write: %.2f' %
                       (sum_t0, sum_t1, sum_t2))
      logging.info('Final_time_stats: read: %.2f predict: %.2f write: %.2f' %
                   (sum_t0, sum_t1, sum_t2))
      table_writer.close()
      logging.info('Predict %s done.' % input_table)

  def predict(self, input_data_dict_list, output_names=None, batch_size=1):
    """Predict input data with loaded model.

    Args:
      input_data_dict_list: list of dict
      output_names:  if not None, will fetch certain outputs, if set None, will
      batch_size: batch_size used to predict, -1 indicates to use the real batch_size

    Return:
      a list of dict, each dict contain a key-value pair for output_name, output_value
    """
    num_example = len(input_data_dict_list)
    assert num_example > 0, 'input data should not be an empty list'
    assert isinstance(input_data_dict_list[0], dict) or \
           isinstance(input_data_dict_list[0], list) or \
           isinstance(input_data_dict_list[0], str), 'input is not a list or dict or str'
    if batch_size > 0:
      num_batches = int(math.ceil(float(num_example) / batch_size))
    else:
      num_batches = 1
      batch_size = len(input_data_dict_list)

    outputs_list = []
    for batch_idx in range(num_batches):
      batch_data_list = input_data_dict_list[batch_idx *
                                             batch_size:(batch_idx + 1) *
                                             batch_size]
      feed_dict = self.batch(batch_data_list)
      outputs = self._predictor_impl.predict(feed_dict, output_names)
      for idx in range(len(batch_data_list)):
        single_result = {}
        for key, batch_value in six.iteritems(outputs):
          single_result[key] = batch_value[idx]
        outputs_list.append(single_result)
    return outputs_list

  def batch(self, data_list):
    """Batching the data."""
    batch_input = {key: [] for key in self._predictor_impl.input_names}
    for data in data_list:
      if isinstance(data, dict):
        for key in data:
          batch_input[key].append(data[key])
      elif isinstance(data, list):
        assert len(self._predictor_impl.input_names) == len(data), \
            'input fields number incorrect, should be %d, but %d' \
            % (len(self._predictor_impl.input_names), len(data))
        for key, v in zip(self._predictor_impl.input_names, data):
          if key != '':
            batch_input[key].append(v)
      elif isinstance(data, str):
        batch_input[self._predictor_impl.input_names[0]].append(data)
    for key in batch_input:
      batch_input[key] = np.array(batch_input[key])
    return batch_input
