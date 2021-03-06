import argparse

import logging
import os
import sys
import cv2

from matplotlib import pyplot as plt

import numpy as np
import torch
import torch.nn as nn
from torch import optim
from tqdm import tqdm
import torchvision

from eval import eval_net
from visualize import visualize_to_tensorboard

from torch.utils.tensorboard import SummaryWriter
from utils.dataset import BasicDataset
from torch.utils.data import DataLoader, random_split

import segmentation_models_pytorch as smp
from thop import profile
import nni

val_iou_score = 0.
best_val_iou_score = 0.
best_test_iou_score = 0.

# huy-pc
# dir_img = '/DATA/hangd/cardi/RobustSegmentation/data/imgs/'
# dir_mask = '/DATA/hangd/cardi/RobustSegmentation/data/masks/'

# dir_img_test = '/DATA/hangd/cardi/RobustSegmentation/data_test/imgs/'
# dir_mask_test = '/DATA/hangd/cardi/RobustSegmentation/data_test/masks/'

# # ailab
# dir_img =  "/data.local/all/hangd/dynamic_data/one32rd/imgs/"
# dir_mask = '/data.local/all/hangd/dynamic_data/one32rd/masks/'

# dir_img_test = '/data.local/all/hangd/src_code_3/Pytorch-UNet/data_test/imgs/'
# dir_mask_test = '/data.local/all/hangd/src_code_3/Pytorch-UNet/data_test/masks/'

# Colab VNU
dir_img =  "/content/n_135/data_train/imgs/"
dir_mask = '/content/n_135/data_train/masks/'

dir_img_test = '/content/n_135/data_test/imgs/'
dir_mask_test = '/content/n_135/data_test/masks/'



def train_net(params,
              dir_checkpoint,
            #   net,
              n_classes,
              bilinear,
              n_channels,
              device,
              epochs=5,
            #   batch_size=1,
            #   lr=0.001,
              val_percent=0.1,
              save_cp=True,
              img_scale=1):
    global best_val_iou_score
    global best_test_iou_score

    _model = {
        "FPN" : smp.FPN, 
        "PSPNet": smp.PSPNet, 
        "Linknet": smp.Linknet, 
        "PAN": smp.PAN,
        "Unet": smp.Unet,
        "DeepLabV3": smp.DeepLabV3,
        "MAnet": smp.MAnet
    }
    _arch = _model.get(params["arch"])
    _encoder = params["encoder"]

    #logging.info(f'encoder_name : {_encoder} ')
    net = _arch(encoder_name=str(_encoder), classes=1 )
    net.to(device=device)

    dataset = BasicDataset(dir_img, dir_mask, img_scale)
    data_test = BasicDataset(dir_img_test, dir_mask_test, img_scale)

    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train, val = random_split(dataset, [n_train, n_val])

    batch_size = params["batch_size"]
    train_loader = DataLoader(train, batch_size=batch_size, shuffle=True, num_workers=4, pin_memory=True)
    train_loader_un_shuffle = DataLoader(train, batch_size=batch_size, shuffle=False, num_workers=2, pin_memory=True)
    val_loader = DataLoader(val, batch_size=batch_size, shuffle=False, num_workers=2, pin_memory=True, drop_last=True)
    test_loader = DataLoader(data_test, batch_size=batch_size, shuffle=False, num_workers=2, pin_memory=True,
                             drop_last=True)

    lr = params["lr"]
    writer = SummaryWriter(comment=f'_{net.__class__.__name__}_LR_{lr}_BS_{batch_size}_SCALE_{img_scale}')
    global_step = 0

    #logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {lr}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_cp}
        Device:          {device.type}
        Images scaling:  {img_scale}
    ''')

    optimizer = optim.RMSprop(net.parameters(), lr=lr, weight_decay=1e-8, momentum=0.9)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min' if n_classes > 1 else 'max', patience=2)
    if n_classes > 1:
        criterion = nn.CrossEntropyLoss()
    else:
        criterion = nn.BCEWithLogitsLoss()

    for epoch in range(epochs):

        net.train()

        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                imgs = batch['image']
                true_masks = batch['mask']
                assert imgs.shape[1] == n_channels, \
                    f'Network has been defined with {n_channels} input channels, ' \
                    f'but loaded images have {imgs.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                imgs = imgs.to(device=device, dtype=torch.float32)
                mask_type = torch.float32 if n_classes == 1 else torch.long
                true_masks = true_masks.to(device=device, dtype=mask_type)

                #logging.info(f'images shape: {imgs.shape}')
                # print("imgs device: ", imgs.device)
                # print("net device ", next(net.parameters()).device)
                masks_pred = net(imgs)
                # print("masks pred shape: ", masks_pred.shape)
                true_masks = true_masks[:, :1, :, :]

                loss = criterion(masks_pred, true_masks)
                epoch_loss += loss.item()
                writer.add_scalar('Loss/train', loss.item(), global_step)

                pbar.set_postfix(**{'loss (batch)': loss.item()})

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_value_(net.parameters(), 0.1)
                optimizer.step()

                pbar.update(imgs.shape[0])
                global_step += 1

                if global_step % (n_train // (2 * batch_size)) == 0:
                    # for tag, value in net.named_parameters():
                    #     tag = tag.replace('.', '/')
                        # writer.add_histogram('weights/' + tag, value.data.cpu().numpy(), global_step)
                        # writer.add_histogram('grads/' + tag, value.grad.data.cpu().numpy(), global_step)
                    val_score, val_iou_score = eval_net(net, val_loader, n_classes, device)
                    if val_iou_score > best_val_iou_score :
                        best_val_iou_score = val_iou_score

                    scheduler.step(val_score)
                    writer.add_scalar('learning_rate', optimizer.param_groups[0]['lr'], global_step)

                    if n_classes > 1:
                        #logging.info('Validation cross entropy: {}'.format(val_score))
                        writer.add_scalar('Loss/val', val_score, global_step)
                    else:
                        #logging.info('Validation Dice Coeff: {}'.format(val_score))
                        writer.add_scalar('Dice/val', val_score, global_step)
                        writer.add_scalar('IOU/val' , val_iou_score, global_step)


                    # T??nh dice v?? iou score tr??n t???p Test set, ghi v??o tensorboard .
                    test_score_dice, test_score_iou = eval_net(net, test_loader, n_classes, device)
                    if test_score_iou > best_test_iou_score :
                        best_test_iou_score = test_score_iou

                    nni.report_intermediate_result(best_test_iou_score)
                    
                    #logging.info('Test Dice Coeff: {}'.format(test_score_dice))
                    writer.add_scalar('Dice/test', test_score_dice, epoch)

                    #logging.info('Test IOU : {}'.format(test_score_iou))
                    writer.add_scalar('IOU/test', test_score_iou, epoch)


        # todo : Using data parallel for the sake of using GPU
        # V??? variance map v?? mean c???a prediction.
        # M???i epoch th?? t??nh l???i uncertainty v?? visualize
        # visualize_to_tensorboard(test_loader, val_loader, train_loader_un_shuffle, writer, device, net, n_channels, n_classes, batch_size, epoch)

        if save_cp:
            try:
                os.mkdir(dir_checkpoint)
                #logging.info('Created checkpoint directory')
            except OSError:
                pass
            torch.save(net.state_dict(),
                       dir_checkpoint + f'CP_epoch{epoch + 1}.pth')
            #logging.info(f'Checkpoint {epoch + 1} saved !')
    nni.report_final_result(best_test_iou_score)
    writer.close()


def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-m', '--method', dest='method', type=str, default='i',
                        help='Choose dropout method: i for MCdropout ; w for Dropconnect')
    parser.add_argument('-cuda', '--cuda-inx', type=int, nargs='?', default=0,
                        help='index of cuda', dest='cuda_inx')                        
    parser.add_argument('-e', '--epochs', metavar='E', type=int, default=100,
                        help='Number of epochs', dest='epochs')
    parser.add_argument('-b', '--batch-size', metavar='B', type=int, nargs='?', default=1,
                        help='Batch size', dest='batchsize')
    parser.add_argument('-lr', '--learning-rate', metavar='LR', type=float, nargs='?', default=0.0001,
                        help='Learning rate', dest='lr')
    parser.add_argument('-f', '--load', dest='load', type=str, default=False,
                        help='Load model from a .pth file')
    parser.add_argument('-s', '--scale', dest='scale', type=float, default=1,
                        help='Downscaling factor of the images')
    parser.add_argument('-v', '--validation', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')

    return parser.parse_args()


if __name__ == '__main__':
    logging.basicConfig(level=#logging.inFO, format='%(levelname)s: %(message)s')
    args = get_args()
    dir_ckp = "ckpt_test/"
    
    if torch.cuda.is_available():
        _device = 'cuda:'+str(args.cuda_inx)
    else: 
        _device = 'cpu'
    device = torch.device(_device)
    #logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    #   - For 1 class and background, use n_classes=1
    #   - For 2 classes, use n_classes=1
    #   - For N > 2 classes, use n_classes=N

    n_classes = 1
    n_channels = 3
    bilinear = True

    
    try:
        params = nni.get_next_parameter()
        train_net(params,
                dir_checkpoint=dir_ckp,
                # net=net,
                n_classes=n_classes,
                bilinear=bilinear,
                n_channels=n_channels,
                epochs=args.epochs,
                # batch_size=args.batchsize,
                # lr=args.lr,
                device=device,
                img_scale=args.scale,
                val_percent=args.val / 100)


    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        #logging.info('Saved interrupt')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
