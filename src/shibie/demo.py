#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.
import argparse
import os
import time
from loguru import logger
# from shibie.srv import Identify, IdentifyResponse
import cv2

import torch
import torch
import sys
sys.path.append("/home/jetson/shibie_ws/src/shibie")
from yolox.data.data_augment import ValTransform
from yolox.data.datasets import COCO_CLASSES
from yolox.exp import get_exp
from yolox.utils import fuse_model, get_model_info, postprocess, vis
from yolox.utils.boxes import poly_postprocess,min_rect
import random
x_p,y_p=0,0
IMAGE_EXT = [".jpg", ".jpeg", ".webp", ".bmp", ".png"]
CLASSES=["坦克","地堡","帐篷","装甲车","桥梁","起飞着陆","无"]
obj=6
#clas="无"


def make_parser():
    parser = argparse.ArgumentParser("YOLOX Demo!")
    parser.add_argument(
        "--demo", default="webcam", help="demo type, eg. image, video and webcam"
    )
    parser.add_argument("-expn", "--experiment-name", type=str, default=None)
    parser.add_argument("-n", "--name", type=str, default=None, help="model name")

    parser.add_argument(
        "--path", default="/home/jetson/shibie_ws/src/shibie/datasets/coco/images", help="path to images or video"
    )
    parser.add_argument("--camid", type=int, default=0, help="webcam demo camera id")
    parser.add_argument(
        "--save_result",
        action="store_true",
        help="whether to save the inference result of image/video",
    )

    # exp file
    parser.add_argument(
        "-f",
        "--exp_file",
        default="/home/jetson/shibie_ws/src/shibie/exps/example/custom/yolox_s.py",
        type=str,
        help="pls input your experiment description file",
    )
    parser.add_argument("-c", "--ckpt", default="/home/jetson/weights/latest_ckpt.pth", type=str, help="ckpt for eval")
    parser.add_argument(
        "--device",
        default="gpu",
        type=str,
        help="device to run our model, can either be cpu or gpu",
    )
    parser.add_argument("--conf", default=0.6, type=float, help="test conf")
    parser.add_argument("--nms", default=0.1, type=float, help="test nms threshold")
    parser.add_argument("--tsize", default=640, type=int, help="test img size")
    parser.add_argument(
        "--fp16",
        dest="fp16",
        default=False,
        action="store_true",
        help="Adopting mix precision evaluating.",
    )
    parser.add_argument(
        "--legacy",
        dest="legacy",
        default=False,
        action="store_true",
        help="To be compatible with older versions",
    )
    parser.add_argument(
        "--fuse",
        dest="fuse",
        default=False,
        action="store_true",
        help="Fuse conv and bn for testing.",
    )
    parser.add_argument(
        "--trt",
        dest="trt",
        default=False,
        action="store_true",
        help="Using TensorRT model for testing.",
    )
    return parser


def get_image_list(path):
    image_names = []
    for maindir, subdir, file_name_list in os.walk(path):
        for filename in file_name_list:
            apath = os.path.join(maindir, filename)
            ext = os.path.splitext(apath)[1]
            if ext in IMAGE_EXT:
                image_names.append(apath)
    return image_names


class Predictor(object):
    def __init__(
        self,
        model,
        exp,
        cls_names=COCO_CLASSES,
        trt_file=None,
        decoder=None,
        device="cpu",
        fp16=False,
        legacy=False,
    ):
        self.model = model
        self.cls_names = cls_names
        self.decoder = decoder
        self.num_apexes = exp.num_apexes
        self.num_classes = exp.num_classes
        #self.num_colors = exp.num_colors
        self.confthre = exp.test_conf
        self.nmsthre = exp.nmsthre
        self.test_size = exp.test_size
        self.device = device
        self.fp16 = fp16
        self.preproc = ValTransform(legacy=legacy)
        if trt_file is not None:
            from torch2trt import TRTModule

            model_trt = TRTModule()
            model_trt.load_state_dict(torch.load(trt_file))

            x = torch.ones(1, 3, exp.test_size[0], exp.test_size[1]).cuda()
            self.model(x)
            self.model = model_trt

    def inference(self, img):
        img_info = {"id": 0}
        if isinstance(img, str):
            img_info["file_name"] = os.path.basename(img)
            img = cv2.imread(img)               # 读取图像
        else:
            img_info["file_name"] = None

        height, width = img.shape[:2]
        img_info["height"] = height
        img_info["width"] = width
        img_info["raw_img"] = img

        ratio = min(self.test_size[0] / img.shape[0], self.test_size[1] / img.shape[1])
        img_info["ratio"] = ratio

        img, _ = self.preproc(img, None, self.test_size)
        img = torch.from_numpy(img).unsqueeze(0)
        img = img.float()
        if self.device == "gpu":
            img = img.cuda()
            if self.fp16:
                img = img.half()  # to FP16

        with torch.no_grad():
            t0 = time.time()
            outputs = self.model(img)   # 前向传播
            if self.decoder is not None:
                outputs = self.decoder(outputs, dtype=outputs.type())
            bbox_preds = []
            #Convert[reg,conf,color,classes] into [bbox,conf,color and classes]
            for i in range(outputs.shape[0]):
                bbox = min_rect(outputs[i,:,:self.num_apexes * 2])
                bbox_preds.append(bbox)
            
            bbox_preds = torch.stack(bbox_preds)

            conf_preds = outputs[:,:,self.num_apexes * 2].unsqueeze(-1)

            cls_preds = outputs[:,:,self.num_apexes * 2 + 1 :]
            # Initialize colors_preds
            # colors_preds = torch.clone(cls_preds)

            # for i in range(self.num_colors):
                # colors_preds[:,:,i * self.num_classes:(i + 1) * self.num_classes] = outputs[:,:,self.num_apexes * 2 + 1 + i:self.num_apexes * 2 + 1 + i + 1].repeat(1, 1, self.num_classes)
                    
            cls_preds_converted =  cls_preds

            outputs_rect = torch.cat((bbox_preds,conf_preds,cls_preds_converted),dim=2)
            outputs_poly = torch.cat((outputs[:,:,:self.num_apexes * 2],conf_preds,cls_preds_converted),dim=2)
            # Out Format: (x1, y1, x2, y2, obj_conf, class_conf, class_pred)
            outputs = poly_postprocess(
                outputs_rect,
                outputs_poly,
                self.num_apexes,
                self.num_classes ,
                self.confthre,
                self.nmsthre
            )
            logger.info("Infer time: {:.4f}s".format(time.time() - t0))
            #print(outputs)
        return outputs, img_info

    def visual(self, output, img_info, cls_conf=0.35):
        global x_p,y_p,obj
        ratio = img_info["ratio"]
        img = img_info["raw_img"]
        if output is None:
            return img,6
        output = output.cpu()

        bboxes = output[:, 0:self.num_apexes*2]
        # preprocessing: resize
        bboxes /= ratio

        cls = output[:, self.num_apexes*2 + 2]
        scores = output[:, self.num_apexes*2] * output[:, self.num_apexes*2 + 1]
       # print(cls)
        vis_res , obj,x_p,y_p = vis(img, bboxes, scores, cls, cls_conf, self.cls_names)
        return vis_res


def image_demo(predictor, vis_folder, path, current_time, save_result):
    if os.path.isdir(path):
        files = get_image_list(path)
    else:
        files = [path]
    files.sort()
    image_name = files[random.randint(0,len(files)-1)]
    outputs, img_info = predictor.inference(image_name)
    result_image , obj = predictor.visual(outputs[0], img_info, predictor.confthre)
    #classes=["坦克","地堡","帐篷","装甲车","桥梁","起飞着陆","无"]
    clas = CLASSES[int(obj)]
    #print(x_p,y_p)
    #print(clas)
    if save_result:
        save_folder = os.path.join(
            vis_folder, time.strftime("%Y_%m_%d_%H_%M_%S", current_time)
        )
        os.makedirs(save_folder, exist_ok=True)
        save_file_name = os.path.join(save_folder, os.path.basename(image_name))
        logger.info("Saving detection result in {}".format(save_file_name))
        cv2.imwrite(save_file_name, result_image)
    ch = cv2.waitKey(0)
    
    return obj


def imageflow_demo(predictor, vis_folder, current_time, args):
    cap = cv2.VideoCapture(args.path if args.demo == "video" else args.camid)
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)  # float
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)  # float
    fps = cap.get(cv2.CAP_PROP_FPS)
    if args.save_result:
        save_folder = os.path.join(
            vis_folder, time.strftime("%Y_%m_%d_%H_%M_%S", current_time)
        )
        os.makedirs(save_folder, exist_ok=True)
        if args.demo == "video":
            save_path = os.path.join(save_folder, args.path.split("/")[-1])
        else:
            save_path = os.path.join(save_folder, "camera.mp4")
        logger.info(f"video save_path is {save_path}")
        vid_writer = cv2.VideoWriter(
            save_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (int(width), int(height))
        )
    while True:
        ret_val, frame = cap.read()
        if ret_val:
            outputs, img_info = predictor.inference(frame)
            result_frame = predictor.visual(outputs[0], img_info, predictor.confthre)
            print(outputs)
            if args.save_result:
                vid_writer.write(result_frame)
            else:
                cv2.namedWindow("yolox", cv2.WINDOW_NORMAL)
                cv2.imshow("yolox", result_frame[0])
            ch = cv2.waitKey(1)
            if ch == 27 or ch == ord("q") or ch == ord("Q"):
                break
        else:
            break


def pre_main(exp, args):
    if not args.experiment_name:                     # 实验名
        args.experiment_name = exp.exp_name

    file_name = os.path.join(exp.output_dir, args.experiment_name)
    os.makedirs(file_name, exist_ok=True)

    vis_folder = None
    if args.save_result:
        vis_folder = os.path.join(file_name, "vis_res")
        os.makedirs(vis_folder, exist_ok=True)

    if args.trt:
        args.device = "gpu"

    logger.info("Args: {}".format(args))            # 日志输出

    if args.conf is not None:
        exp.test_conf = args.conf
    if args.nms is not None:
        exp.nmsthre = args.nms
    if args.tsize is not None:
        exp.test_size = (args.tsize, args.tsize)

    model = exp.get_model()                         # 加载模型
    logger.info("Model Summary: {}".format(get_model_info(model, exp.test_size)))

    if args.device == "gpu":                        # 使用GPU
        model.cuda()
        if args.fp16:
            model.half()  # to FP16
    model.eval()

    if not args.trt:
        if args.ckpt is None:
            ckpt_file = os.path.join(file_name, "best_ckpt.pth")
        else:
            ckpt_file = args.ckpt
        logger.info("loading checkpoint")
        ckpt = torch.load(ckpt_file, map_location="cpu")
        # load the model state dict
        model.load_state_dict(ckpt["model"])
        logger.info("loaded checkpoint done.")

    if args.fuse:
        logger.info("\tFusing model...")
        model = fuse_model(model)

    if args.trt:
        assert not args.fuse, "TensorRT model is not support model fusing!"
        trt_file = os.path.join(file_name, "model_trt.pth")
        assert os.path.exists(
            trt_file
        ), "TensorRT model is not found!\n Run python3 tools/trt.py first!"
        model.head.decode_in_inference = False
        decoder = model.head.decode_outputs
        logger.info("Using TensorRT to inference")
    else:
        trt_file = None
        decoder = None

    predictor = Predictor(model, exp, COCO_CLASSES, trt_file, decoder, args.device, args.fp16, args.legacy) # 创建Predictor类对象
    
    return predictor, vis_folder


def main(predictor, vis_folder, args):
    current_time = time.localtime()
    if args.demo == "image":
        target= image_demo(predictor, vis_folder, args.path, current_time, args.save_result)
    elif args.demo == "video" or args.demo == "webcam":
        target = imageflow_demo(predictor, vis_folder, current_time, args)
    
    return target,x_p,y_p


if __name__ == "__main__":
    args = make_parser().parse_args()
    exp = get_exp(args.exp_file, args.name)

    predictor, vis_folder = pre_main(exp, args)
    main(predictor, vis_folder, args)
