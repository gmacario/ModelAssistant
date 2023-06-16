import os
import time
import os.path as osp
from typing import List, AnyStr, Tuple, Optional, Union

import cv2
import mmcv
import onnx
import torch
import numpy as np
from tqdm.std import tqdm
from torch.utils.data import DataLoader

from mmdet.models.utils import samplelist_boxtype2tensor
from mmengine.config import Config
from mmengine.evaluator import Evaluator
from mmengine.structures import InstanceData
from mmengine.registry import MODELS
from edgelab.models.utils.computer_acc import pose_acc, audio_acc

from edgelab.utils.cv import load_image, NMS


class Inter():

    def __init__(self, model: List or AnyStr or Tuple):
        if isinstance(model, list):
            try:
                import ncnn
            except:
                raise ImportError(
                    'You have not installed ncnn yet, please execute the "pip install ncnn" command to install and run again'
                )
            net = ncnn.Net()
            for p in model:
                if p.endswith('param'): param = p
                if p.endswith('bin'): bin = p
            net.load_param(param)
            net.load_model(bin)
            # net.opt.use_vulkan_compute = True
            self.engine = 'ncnn'
        elif model.endswith('onnx'):
            try:
                import onnxruntime
            except:
                raise ImportError(
                    'You have not installed onnxruntime yet, please execute the "pip install onnxruntime" command to install and run again'
                )
            try:
                net = onnx.load(model)
                onnx.checker.check_model(net)
            except:
                raise ValueError(
                    'onnx file have error,please check your onnx export code!')
            providers = [
                'CUDAExecutionProvider', 'CPUExecutionProvider'
            ] if torch.cuda.is_available() else ['CPUExecutionProvider']
            net = onnxruntime.InferenceSession(model, providers=providers)

            self._input_shape = net.get_inputs()[0].shape[1:]
            channels = self._input_shape.pop(0)
            self._input_type = net.get_inputs()[0].type
            self._input_shape.append(channels)
            self.engine = 'onnx'
        elif model.endswith('tflite'):
            try:
                import tensorflow as tf
            except:
                raise ImportError(
                    'You have not installed tensorflow yet, please execute the "pip install tensorflow" command to install and run again'
                )
            inter = tf.lite.Interpreter
            net = inter(model)
            self._input_shape = list(net.get_input_details()[0]['shape'][1:])
            net.allocate_tensors()
            self.engine = 'tf'
        else:
            raise 'model file input error'
        self.inter = net

    @property
    def input_shape(self):
        return self._input_shape

    def __call__(self,
                 img: Union[np.array, torch.Tensor],
                 input_name: AnyStr = 'input',
                 output_name: AnyStr = 'output',
                 result_num=1):
        # img.resize_(3,192,192)
        if len(img.shape) == 2:  # audio
            if img.shape[1] > 10:  # (1, 8192) to (8192, 1)
                img = img.transpose(1, 0) if self.engine == 'tf' else img
            img = np.array([img])  # add batch dim.
        elif len(img.shape) == 3:
            C, H, W = img.shape
            if C not in [1, 3]:
                img = img.transpose(2, 0, 1)
            if isinstance(img, torch.Tensor):
                img = img.numpy()
            img = np.array([img])  # add batch dim.
        elif len(img.shape) == 4:
            B, C, H, W = img.shape
            if C not in [1, 3]:
                img = img.transpose(0, 3, 1, 2)
            if isinstance(img, torch.Tensor):
                img = img.numpy()

        else:  # error
            raise ValueError
        results = []
        if self.engine == 'onnx':  # onnx
            result = self.inter.run(
                [self.inter.get_outputs()[0].name],
                {self.inter.get_inputs()[0].name: img})[0][0]
            results.append(result)
        elif self.engine == 'ncnn':  # ncnn
            self.inter.opt.use_vulkan_compute = False
            extra = self.inter.create_extractor()
            extra.input(input_name, ncnn.Mat(img[0]))
            result = extra.extract(output_name)[1]
            result = [result[i] for i in range(len(result))]
        else:  # tf
            input_, outputs = self.inter.get_input_details()[0], (
                self.inter.get_output_details()[0] for i in range(result_num))
            int8 = input_['dtype'] == np.int8 or input_['dtype'] == np.uint8
            img = img.transpose(0, 2, 3, 1) if len(img.shape) == 4 else img
            if int8:
                scale, zero_point = input_['quantization']
                img = (img / scale + zero_point).astype(np.int8)
            self.inter.set_tensor(input_['index'], img)
            self.inter.invoke()
            for output in outputs:
                result = self.inter.get_tensor(output['index'])
                if int8:
                    scale, zero_point = output['quantization']
                    result = (result.astype(np.float32) - zero_point) * scale
                results.append(result)

        return results


img_suff = ('.jpg', '.png', '.PNG', '.JPEG')


class DataStream:

    def __init__(self,
                 source: Union[int, str],
                 shape: Optional[int or Tuple[int, int]] = None) -> None:
        if shape:
            self.gray = True if shape[-1] == 1 else False
            self.shape = shape[:-1]
        else:
            self.shape = shape
        self.file = None
        self.l = 0

        if isinstance(source, str):
            if osp.isdir(source):
                self.file = [
                    osp.join(source, f) for f in os.listdir(source)
                    if f.endswith(img_suff)
                ]

                self.l = len(self.file)
                self.file = iter(self.file)

            elif osp.isfile(source):
                self.file = [source]
                self.l = len(self.file)
                self.file = iter(self.file)
            elif source.isdigit():
                self.cap = cv2.VideoCapture(int(source))
            else:
                raise
        elif isinstance(source, int):
            self.cap = cv2.VideoCapture(source)
        else:
            raise

    def __len__(self):
        return self.l if self.file else None

    def __iter__(self):
        return self

    def __next__(self):
        if self.file:
            f = next(self.file)
            img = load_image(f,
                             shape=self.shape,
                             mode='GRAY' if self.gray else 'RGB',
                             normalized=True)

        else:
            ret, img = self.cap.read()

            if self.gray:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                img = np.expand_dims(img, axis=-1)

            if self.shape:
                img = cv2.resize(img, self.shape[::-1])

            img = (img / 255).astype(np.float32)

        return img


class Infernce:
    """
    Model Reasoning Test
    Reasonable onnx, tflite, ncnn and other models
    """

    def __init__(self,
                 model: List or AnyStr or Tuple,
                 dataloader: Union[DataLoader, str, int, None] = None,
                 cfg: Optional[Config] = None,
                 runner=None,
                 source: Optional[str] = None,
                 task: str = 'det',
                 show: bool = False,
                 save_dir: Optional[str] = None,
                 audio: bool = False) -> None:

        # chaeck source data
        assert not (source is None and dataloader is None
                    ), 'Both source and dataload cannot be None'

        self.class_name = dataloader.dataset.METAINFO['classes']
        # load model
        self.model = Inter(model)
        # make dataloader
        self.source = source
        self.runner = runner

        if source:
            self.dataloader = DataStream(source, shape=self.model.input_shape)
        else:
            self.dataloader = dataloader

        self.cfg = cfg
        self.show = show
        self.task = task
        self.save_dir = save_dir
        self.input_shape = self.model.input_shape
        self.init(cfg)

    def init(self, cfg):
        self.evaluator: Evaluator = self.runner.build_evaluator(
            self.cfg.get('val_evaluator'))
        if hasattr(cfg.model, "data_preprocessor"):
            self.data_preprocess = MODELS.build(cfg.model.data_preprocessor)

    def post_process(self):
        pass

    def test(self) -> None:
        self.time_cost = 0
        self.preds = []

        for data in tqdm(self.dataloader):

            if not self.source:
                if hasattr(self, "data_preprocess"):
                    data = self.data_preprocess(data, True)
                inputs = data['inputs'][0]
                img_path = data['data_samples'][0].img_path
                img = None
                img = data['inputs'][0].permute(1, 2, 0).cpu().numpy()
            else:
                img = data
                inputs = data
                img_path = None

            t0 = time.time()
            preds = self.model(inputs)
            self.time_cost += time.time() - t0

            result = InstanceData()
            if self.task == 'pose':
                show_point(preds, data['data_samples']['image_file'][0])
            elif self.task == 'det':
                if len(preds[0].shape) > 2:
                    preds = preds[0][0]
                elif len(preds[0].shape) == 2:
                    preds = preds[0]
                else:
                    Warning("!!!")

                # performes nms
                bbox, conf, classes = preds[:, :4], preds[:, 4], preds[:, 5:]
                preds = NMS(bbox,
                            conf,
                            classes,
                            conf_thres=25,
                            bbox_format='xywh')
                # show det result and save result
                show_det(preds,
                         img=img,
                         img_file=img_path,
                         class_name=self.class_name,
                         shape=self.input_shape[:-1],
                         show=self.show,
                         save_path=self.save_dir)

                if not self.source:

                    ori_shape = data['data_samples'][0].ori_shape
                    tmp = preds[:, :4]
                    tmp[:,
                        0::2] = tmp[:,
                                    0::2] / self.input_shape[1] * ori_shape[1]
                    tmp[:,
                        1::2] = tmp[:,
                                    1::2] / self.input_shape[0] * ori_shape[0]
                    result.bboxes = tmp
                    result.scores = preds[:, 4]
                    result.labels = preds[:, 5].type(torch.int)
                    # result.img_id = str(data['data_samples'][0].img_id)

                    for data_sample, pred_instances in zip(
                            data['data_samples'], [result]):
                        data_sample.pred_instances = pred_instances
                    samplelist_boxtype2tensor(data)

                    self.evaluator.process(data_batch=data,
                                           data_samples=data['data_samples'])

            else:
                raise ValueError
        if not self.source:
            self.evaluator.evaluate(len(self.dataloader.dataset))
        print(f"FPS: {len(self.dataloader)/self.time_cost:2f} fram/s")


def pfld_inference(model, data_loader):
    results = []
    prog_bar = mmcv.ProgressBar(len(data_loader))
    for data in data_loader:
        # parse data
        input = data.dataset['img']
        target = np.expand_dims(data.dataset['keypoints'], axis=0)
        size = data.dataset['hw']  #.cpu().numpy()
        input = input.cpu().numpy()
        result = model(input)
        result = np.array(result)
        result = result if len(result.shape) == 2 else result[
            None, :]  # onnx shape(2,), tflite shape(1,2)
        acc = pose_acc(result.copy(), target, size)
        results.append({
            'Acc': acc,
            'pred': result,
            'image_file': data.dataset['image_file'].data
        })

        prog_bar.update()
    return results


def audio_inference(model, data_loader):
    results = []
    prog_bar = mmcv.ProgressBar(len(data_loader))
    for data in data_loader:
        # parse data
        input = data.dataset['audio']
        target = data.dataset['labels']
        input = input.cpu().numpy()
        result = model(input)
        # result = result if len(result.shape)==2 else np.expand_dims(result, 0) # onnx shape(d,), tflite shape(1,d)
        # result = result[0] if len(result.shape)==2 else result
        acc = audio_acc(result, target)
        results.append({
            'acc': acc,
            'pred': result,
            'image_file': data.dataset['audio_file']
        })
        prog_bar.update()
    return results


def fomo_inference(model, data_loader):
    results = []
    prog_bar = mmcv.ProgressBar(len(data_loader))
    for data in data_loader:
        input = data.dataset['img']
        input = input.cpu().numpy()
        target = data.dataset['target']
        result = model(input)
        results.append({
            'pred': result,
            'target': target,
        })
        prog_bar.update()
    return results


def show_point(keypoints,
               img_file,
               win_name='test',
               save_path=False,
               not_show=False):
    img = mmcv.imread(img_file, channel_order='bgr').copy()
    h, w = img.shape[:-1]
    keypoints = keypoints[0] if len(keypoints.shape) == 2 else keypoints
    keypoints[::2] = keypoints[::2] * w
    keypoints[1::2] = keypoints[1::2] * h

    for idx, point in enumerate(keypoints[::2]):
        if not isinstance(point, (float, int)):
            img = cv2.circle(img, (int(point), int(keypoints[idx * 2 + 1])), 2,
                             (255, 0, 0), -1)
    if not not_show:
        cv2.imshow(win_name, img)
        cv2.waitKey(500)

    if save_path:
        img_name = osp.basename(img_file)
        cv2.imwrite(osp.join(save_path, img_name), img)


def show_det(pred: np.ndarray,
             img: Optional[np.ndarray] = None,
             img_file: Optional[str] = None,
             win_name='Detection',
             class_name=None,
             shape=None,
             save_path=False,
             show=False) -> np.ndarray:

    assert not (img is None and img_file is None
                ), "The img and img_file parameters cannot both be None"

    # load image
    if isinstance(img, np.ndarray):
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        img = load_image(img_file, shape=shape, mode='BGR').copy()

    # plot the result
    for i in pred:
        x1, y1, x2, y2 = map(int, i[:4])
        img = cv2.rectangle(img, (x1, y1), (x2, y2), (255, 0, 0), 1)
        cv2.putText(img,
                    class_name[int(i[5])], (x1, y1),
                    1,
                    color=(0, 0, 255),
                    thickness=1,
                    fontScale=1)
        cv2.putText(img,
                    str(round(i[4].item(), 2)), (x1, y1 - 15),
                    1,
                    color=(0, 0, 255),
                    thickness=1,
                    fontScale=1)

    if show:
        cv2.imshow(win_name, img)
        cv2.waitKey(500)

    if save_path:
        img_name = osp.basename(img_file)
        cv2.imwrite(osp.join(save_path, img_name), img)
    return pred


if __name__ == "__main__":
    data = DataStream(0)
    data = iter(data)
    for img in data:
        cv2.imshow('aaa', img)
        cv2.waitKey(0)