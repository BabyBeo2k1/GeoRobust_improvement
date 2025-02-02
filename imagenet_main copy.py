import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, Dataset
from torchvision.utils import save_image
import numpy as np

import os
import argparse
import time

from direct_with_lb import LowBoundedDIRECT, LowBoundedDIRECT_POset_full_parrallel
from geo_transf_verifications import GeometricVarification, AffineTransf, _3_channel_obstacle_bound, make_theta, reachability_loss, cw_loss
from imagenet_utils import prepare_img, tss_transform, load_model, load_timm_model


def cw_loss(x, y):
    x_sorted, ind_sorted = x.sort(dim=1)
    ind = (ind_sorted[:, -1] == y).float()
    
    loss_value = (x[np.arange(x.shape[0]), y] - x_sorted[:, -2] * ind - x_sorted[:, -1] * (1. - ind))
    return loss_value

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--example-idx', default=506, type=int)
    parser.add_argument('--data-dir', default='/datasets/ImageNet2012/vaild', type=str)
    parser.add_argument('--model-dir', default='./model', type=str)
    parser.add_argument('--model-name', default='resnet50', type=str)
    parser.add_argument('--device', default='cuda', type=str)
    parser.add_argument('--timm-model', action='store_true')
    parser.add_argument('--tss', action='store_true')

    # Transformation
    parser.add_argument('--angle', default=0.0, type=float)
    parser.add_argument('--shift', default=0, type=float)
    parser.add_argument('--scale', default=0.0, type=float)
    parser.add_argument('--obstacle', action='store_true')
    parser.add_argument('--l-inf-bound', default=0.3, type=float)
    parser.add_argument('--topleft-x', default=0, type=int)
    parser.add_argument('--topleft-y', default=0, type=int)
    parser.add_argument('--width', default=0, type=int)
    parser.add_argument('--height', default=0, type=int)

    # DIRECT
    parser.add_argument('--max-evaluation', default=3000, type=int)
    parser.add_argument('--max-deep', default=6, type=int)
    parser.add_argument('--max-iteration', default=50, type=int)
    parser.add_argument('--tolerance', default=1e-4, type=float)
    parser.add_argument('--po-set', action='store_true')
    parser.add_argument('--po-set-size', default=2, type=int)
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--cw', action='store_true')
    return parser.parse_args()


def main():
    args = get_args()
    testset = torchvision.datasets.ImageFolder(args.data_dir)
    raw_img, label = testset[args.example_idx]
    if args.timm_model:
        model = load_timm_model(args.model_name, args.device)
        model.eval()
        img = prepare_img(raw_img, args.model_name)
    elif args.tss:
        from tss_utils import load_tss_model
        model = load_tss_model("resnet50", "imagenet", os.path.join(args.model_dir, args.model_name))       
        model.eval()
        img = tss_transform(raw_img)
    else:
        raise NotImplementedError()
    data_size = tuple(img.shape)
    ori_out = model(img.unsqueeze(0).to(args.device))
    if args.cw:
        ori_conf = cw_loss(ori_out, label).item()
    else:
        ori_conf = reachability_loss(ori_out, label).item()
    if torch.argmax(ori_out).item() == label:
        correctness = 1
    else:
        correctness = 0

    if (args.cw and correctness!= 1): 
        print(f'example {args.example_idx} is misclassified, pass')
        raise AssertionError()
    
    if args.obstacle:
        transf = 'obstacle'
        nb_pixel = args.width * args.height
        assert nb_pixel != 0 and args.l_inf_bound > 0
        location_dist = {
            'tl_x':args.topleft_x,
            'tl_y':args.topleft_y,
            'width':args.width,
            'height':args.height,
        }
        bound = _3_channel_obstacle_bound(img, args.topleft_x, args.topleft_y, args.width, args.height, args.l_inf_bound)

    else:
        transf = []
        bound = []
        location_dist = {}
        if args.angle != 0:
            transf.append('angle')
            bound.append([-np.pi*args.angle, np.pi*args.angle])
        if args.shift != 0:
            transf.append('h_shift'); transf.append('v_shift')
            bound.append([-args.shift, args.shift])
            bound.append([-args.shift, args.shift])
        if args.scale != 0:
            transf.append('scale')
            bound.append([1-args.scale, 1+args.scale])
    assert len(bound) != 0

    if args.cw:
        task = GeometricVarification(model, img, data_size, label, cw_loss, args.device, transf, **location_dist)
    else:
        task = GeometricVarification(model, img, data_size, label, reachability_loss, args.device, transf, **location_dist)

    object_func = task.set_problem()
    if args.po_set:
        direct_solver = LowBoundedDIRECT_POset_full_parrallel(object_func,args.example_idx, len(bound), bound, args.max_iteration, args.max_deep, args.max_evaluation, args.tolerance, args.po_set_size, debug=args.debug)
    else:
        direct_solver = LowBoundedDIRECT(object_func,args.example_idx, len(bound), bound, args.max_iteration, args.max_deep, args.max_evaluation, args.tolerance,debug=args.debug)
    start_time = time.time()
    direct_solver.solve()
    end_time = time.time()

    if transf != 'obstacle':
        opt_theta = make_theta(transf, direct_solver.optimal_result())
        optimal_transf = AffineTransf(opt_theta)
        optimal_img = optimal_transf(img.unsqueeze(0))
    else:
        patch = torch.zeros(img.squeeze().shape)
        patch[:,args.topleft_y:args.topleft_y+args.height,args.topleft_x:args.topleft_x+args.width] = torch.tensor(direct_solver.optimal_result()).view(data_size[0], args.height, args.width)
        optimal_img = (img+patch).unsqueeze(0)

    opt_out = model(optimal_img.to(args.device))
    if torch.argmax(opt_out).item() == label:
        post_correctness = 1
    else:
        post_correctness = 0
    # print(f'example: {args.example_idx},correctness:{correctness}, conf{ori_conf:.6f},post_correctness: {post_correctness},min:{direct_solver.rcd.minimum:.6f},last center:{direct_solver.rcd.last_center+1},best idx:{direct_solver.rcd.best_idx},low bound: {direct_solver.local_low_bound:.6f}, largest slope: {direct_solver.get_largest_slope():.1f},opt size: {direct_solver.get_opt_size()}, largest po size:{direct_solver.get_largest_po_size()},time:{(end_time - start_time):.2f}')
    # print(f'{list(direct_solver.optimal_result())}')

if __name__ == '__main__':
    main()