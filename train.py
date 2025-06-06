import os

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = '0,1,2,3'

import torch

torch.backends.cudnn.benchmark = True

import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader

import random
import time
import numpy as np

import utils
from data_RGB import get_training_data, get_validation_data
from model_my import MultiscaleNet as myNet
#from model_S import MultiscaleNet as myNet
import losses
from warmup_scheduler import GradualWarmupScheduler
from tqdm import tqdm
from get_parameter_number import get_parameter_number
import kornia
from torch.utils.tensorboard import SummaryWriter
import argparse
from accelerate import Accelerator
from skimage import img_as_ubyte
from torch.nn.parallel import DistributedDataParallel as DDP

######### Set Seeds ###########
random.seed(1234)
np.random.seed(1234)
torch.manual_seed(1234)
# torch.cuda.manual_seed_all(1234)
accelerator = Accelerator()
start_epoch = 1

parser = argparse.ArgumentParser(description='Image Deraininig')

parser.add_argument('--train_dir', default='/home/t2vg-a100-G4-42/v-shuyuantu/NeRD-Rain/Datasets/4combine-481x321/train', type=str, help='Directory of train images')
parser.add_argument('--val_dir', default='/home/t2vg-a100-G4-42/v-shuyuantu/NeRD-Rain/Datasets/4combine-481x321/test', type=str, help='Directory of validation images')
parser.add_argument('--model_save_dir', default='/home/t2vg-a100-G4-42/v-shuyuantu/NeRD-Rain/new_checkpoints/', type=str, help='Path to save weights')
parser.add_argument('--pretrain_weights', default='', type=str, help='Path to pretrain-weights')
parser.add_argument('--mode', default='Deraininig', type=str)
parser.add_argument('--session', default='Multiscale', type=str, help='session')
parser.add_argument('--patch_size', default=256, type=int, help='patch size')
parser.add_argument('--num_epochs', default=100, type=int, help='num_epochs')
parser.add_argument('--batch_size', default=16, type=int, help='batch_size')
parser.add_argument('--val_epochs', default=10, type=int, help='val_epochs')
args = parser.parse_args()

mode = args.mode
session = args.session
patch_size = args.patch_size

model_dir = os.path.join(args.model_save_dir, mode, 'models', session)
utils.mkdir(model_dir)

train_dir = args.train_dir
val_dir = args.val_dir

num_epochs = args.num_epochs
batch_size = args.batch_size
val_epochs = args.val_epochs

start_lr = 1e-4
end_lr = 1e-6

######### Model ###########
model_restoration = myNet()

# print number of model
get_parameter_number(model_restoration)

# Remove the manual DDP wrapping - let Accelerator handle it
optimizer = optim.Adam(model_restoration.parameters(), lr=start_lr, betas=(0.9, 0.999), eps=1e-8)

######### Scheduler ###########
warmup_epochs = 3
scheduler_cosine = optim.lr_scheduler.CosineAnnealingLR(optimizer, num_epochs - warmup_epochs, eta_min=end_lr)
scheduler = GradualWarmupScheduler(optimizer, multiplier=1, total_epoch=warmup_epochs, after_scheduler=scheduler_cosine)

RESUME = False
Pretrain = False
model_pre_dir = ''

######### Pretrain ###########
if Pretrain:
    utils.load_checkpoint(model_restoration, model_pre_dir)

    print('------------------------------------------------------------------------------')
    print("==> Retrain Training with: " + model_pre_dir)
    print('------------------------------------------------------------------------------')

######### Resume ###########
if RESUME:
    path_chk_rest = utils.get_last_path(model_dir, '_latest.pth')
    utils.load_checkpoint(model_restoration, path_chk_rest)
    start_epoch = utils.load_start_epoch(path_chk_rest) + 1
    utils.load_optim(optimizer, path_chk_rest)

    for i in range(1, start_epoch):
        scheduler.step()
    new_lr = scheduler.get_lr()[0]
    print('------------------------------------------------------------------------------')
    print("==> Resuming Training with learning rate:", new_lr)
    print('------------------------------------------------------------------------------')

######### Loss ###########
criterion_char = losses.CharbonnierLoss()
criterion_edge = losses.EdgeLoss()
criterion_fft = losses.fftLoss()
criterion_L1 = nn.L1Loss(size_average=True)

######### DataLoaders ###########
train_dataset = get_training_data(train_dir, {'patch_size': patch_size})
train_loader = DataLoader(dataset=train_dataset, batch_size=batch_size, shuffle=True, num_workers=8, drop_last=False,
                          pin_memory=True)

val_dataset = get_validation_data(val_dir, {'patch_size': patch_size})
val_loader = DataLoader(dataset=val_dataset, batch_size=1, shuffle=False, num_workers=8, drop_last=False,
                        pin_memory=True)

# Move the accelerator.prepare() call here, before any model usage
model_restoration, optimizer, train_loader, val_loader = accelerator.prepare(
    model_restoration, optimizer, train_loader, val_loader
)

print('===> Start Epoch {} End Epoch {}'.format(start_epoch, num_epochs + 1))
print('===> Loading datasets')

best_psnr = 0
best_epoch = 0
writer = SummaryWriter(model_dir)
iter = 0

for epoch in range(start_epoch, num_epochs + 1):
    epoch_start_time = time.time()
    epoch_loss = 0
    train_id = 1

    model_restoration.train()
    for i, data in enumerate(tqdm(train_loader), 0):

        # zero_grad
        for param in model_restoration.parameters():
            param.grad = None

        target_ = data[0] #.cuda()
        input_ = data[1] #.cuda()
        device = input_.device
        target = kornia.geometry.transform.build_pyramid(target_, 3)
        target = [t.to(device) for t in target]
        restored = model_restoration(input_)

        device = input_.device  # 一般是 accelerator.prepare 后 input_ 所在的 device

        loss_fft = (
            criterion_fft(restored[0], target[0]) +
            criterion_fft(restored[1], target[1]) +
            criterion_fft(restored[2], target[2])
        ).to(device)

        loss_char = (
            criterion_char(restored[0], target[0]) +
            criterion_char(restored[1], target[1]) +
            criterion_char(restored[2], target[2])
        ).to(device)

        loss_edge = (
            criterion_edge(restored[0], target[0]) +
            criterion_edge(restored[1], target[1]) +
            criterion_edge(restored[2], target[2])
        ).to(device)

        loss_l1 = (
            criterion_L1(restored[3], target[1]) +
            criterion_L1(restored[5], target[2])
        ).to(device)
        loss = loss_char + 0.01 * loss_fft + 0.05 * loss_edge + 0.1 * loss_l1
        
        # ====== 关键加这里 ======
        dummy_loss = 0
        for x in restored:
            dummy_loss += (x.sum() * 0)
        loss = loss + dummy_loss
        
        for name, param in model_restoration.named_parameters():
            if param.requires_grad:
                loss = loss + 0.0 * param.sum()
        # ====== 到这里 ======
        # loss.backward()
        accelerator.backward(loss)
        optimizer.step()
        epoch_loss += loss.item()
        iter += 1
        writer.add_scalar('loss/fft_loss', loss_fft, iter)
        writer.add_scalar('loss/char_loss', loss_char, iter)
        writer.add_scalar('loss/edge_loss', loss_edge, iter)
        writer.add_scalar('loss/l1_loss', loss_l1, iter)
        writer.add_scalar('loss/iter_loss', loss, iter)
    writer.add_scalar('loss/epoch_loss', epoch_loss, epoch)
    #### Evaluation ####
    if epoch % val_epochs == 0:
        model_restoration.eval()
        psnr_val_rgb = []
        for ii, data_val in enumerate((val_loader), 0):
            target = data_val[0] #.cuda()
            input_ = data_val[1] #.cuda()

            with torch.no_grad():
                restored = model_restoration(input_)

            for res, tar in zip(restored[0], target):
                psnr_val_rgb.append(utils.torchPSNR(res, tar))

        psnr_val_rgb = torch.stack(psnr_val_rgb).mean().item()
        writer.add_scalar('val/psnr', psnr_val_rgb, epoch)
        if psnr_val_rgb > best_psnr:
            best_psnr = psnr_val_rgb
            best_epoch = epoch
            torch.save({'epoch': epoch,
                        'state_dict': model_restoration.state_dict(),
                        'optimizer': optimizer.state_dict()
                        }, os.path.join(model_dir, "model_best.pth"))

        print("[epoch %d PSNR: %.4f --- best_epoch %d Best_PSNR %.4f]" % (epoch, psnr_val_rgb, best_epoch, best_psnr))

        torch.save({'epoch': epoch,
                    'state_dict': model_restoration.state_dict(),
                    'optimizer': optimizer.state_dict()
                    }, os.path.join(model_dir, f"model_epoch_{epoch}.pth"))

    scheduler.step()

    print("------------------------------------------------------------------")
    print("Epoch: {}\tTime: {:.4f}\tLoss: {:.4f}\tLearningRate {:.6f}".format(epoch, time.time() - epoch_start_time,epoch_loss, scheduler.get_lr()[0]))
    print("------------------------------------------------------------------")

    torch.save({'epoch': epoch,
                'state_dict': model_restoration.state_dict(),
                'optimizer': optimizer.state_dict()
                }, os.path.join(model_dir, "model_latest.pth"))

writer.close()
