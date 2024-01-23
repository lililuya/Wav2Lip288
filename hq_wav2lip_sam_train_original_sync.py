from os.path import dirname, join, basename, isfile
import sys

import time
import datetime
from lpips import LPIPS
# from models import SyncNet_color_384 as SyncNet

from models import SyncNet_color as SyncNet
from models import Wav2Lip_SAM as Wav2Lip, NLayerDiscriminator

import  torch
import  logging
from    torch import nn
from    torch.nn import functional as F
from    torch import optim
from    torch.utils import data as data_utils
import  numpy as np
import  os, random, cv2, argparse
from    pytorch_lightning.loggers import CSVLogger
from    dataloader_wav2lip import Dataset
from hparams import hparams, get_image_list

parser = argparse.ArgumentParser(description='Code to train the Wav2Lip model WITH the visual quality discriminator')

parser.add_argument('--checkpoint_dir', help='Save checkpoints to this directory', required=False, default="checkpoints/wav2lip/", type=str)
parser.add_argument('--log_dir', help='Write log files to this directory', required=False, default="logs/wav/", type=str)
parser.add_argument('--exp_num', help='ID number of the experiment', required=False, default="sam", type=str)
parser.add_argument('--syncnet_checkpoint_path', help='Load the pre-trained Expert discriminator', default="/mnt/sdb/liwen/wav2lip_288x288/checkpoints/sync_original/lipsync_expert.pth", required=False, type=str)
parser.add_argument('--checkpoint_path', help='', default="/mnt/sdb/liwen/wav2lip_288x288/checkpoints/wav2lip/sam/gen_checkpoint_384_4_000096000_2024-01-01.pth", type=str)
parser.add_argument('--disc_checkpoint_path', help='Resume quality disc from this checkpoint', default="/mnt/sdb/liwen/wav2lip_288x288/checkpoints/wav2lip/sam/disc_checkpoint_384_4_000096000_2024-01-01.pth", type=str)

args = parser.parse_args()

global_step = 0
global_epoch = 0
use_cuda = torch.cuda.is_available()
best_loss = 10000
print('use_cuda: {}'.format(use_cuda))

syncnet_T = 5
syncnet_mel_step_size = 16
disc_iter_start = 30000
sync_iter_start = 60000
hparams.set_hparam('img_size', 384)


def save_sample_images(x, g, gt, vidname, global_step, checkpoint_dir):
    x = (x.detach().cpu().numpy().transpose(0, 2, 3, 4, 1) * 255.0).astype(np.uint8)
    g = (g.detach().cpu().numpy().transpose(0, 2, 3, 4, 1) * 255.0).astype(np.uint8)
    gt = (gt.detach().cpu().numpy().transpose(0, 2, 3, 4, 1) * 255.0).astype(np.uint8)

    refs, inps = x[..., 3:], x[..., :3]
    folder = join(checkpoint_dir, "samples_step{:09d}".format(global_step))
    if not os.path.exists(folder): os.mkdir(folder)
    collage = np.concatenate((refs, inps, g, gt), axis=-2)
    for batch_idx, c in enumerate(collage):
        image_id = vidname[batch_idx].split('/')[-2]
        print(image_id)
        for t in range(len(c)):
            cv2.imwrite('{}/{}_{}.jpg'.format(folder, image_id, t), c[t], [cv2.IMWRITE_JPEG_QUALITY, 100])


logloss = nn.BCELoss()
# logloss = nn.MSELoss()
def cosine_loss(a, v, y):
    d = nn.functional.cosine_similarity(a, v)
    loss = logloss(d.unsqueeze(1), y)
    return loss

device = torch.device("cuda" if use_cuda else "cpu")

recon_loss = nn.L1Loss()

def get_sync_loss(mel, g, syncnet):
    # print("Syncing", g.shape, gt.shape)
    if syncnet is None:
        return torch.Tensor([10])
    # print(g.shape)
    g = g.view(-1, 1, 384, 384)
    target_szie = (96, 96)
    g = F.interpolate(g, size=target_szie, mode='bilinear', align_corners=False)
    g = g.view(4,3,5,96,96)
    # ([16, 3, 5, 96, 96]) B C T 96 96
    g = g[:, :, :, g.size(3)//2:]
    g = torch.cat([g[:, :, i] for i in range(syncnet_T)], dim=1)
    # print(g.shape) # torch.Size([4, 15, 192, 384])
    # B, 3 * T, H//2, W
    # print("/****",g.shape)
    a, v = syncnet(mel, g)
    y = torch.ones(g.size(0), 1).float().to(device)
    return cosine_loss(a, v, y)



def hinge_d_loss(logits_real, logits_fake):
    loss_real = torch.mean(F.relu(1. - logits_real))
    loss_fake = torch.mean(F.relu(1. + logits_fake))
    return loss_real, loss_fake


def train(device, model, disc, train_data_loader, test_data_loader, optimizer, disc_optimizer, checkpoint_dir=None, checkpoint_interval=None, nepochs=None, log_interval=None,syncnet=None):
    global global_step, global_epoch
    resumed_step = global_step

    if not os.path.isdir(args.log_dir): os.makedirs(args.log_dir)
    logger      = CSVLogger(args.log_dir, name=f"train{args.exp_num}")
    valLogger   = CSVLogger(args.log_dir, name=f"val{args.exp_num}")
    # bce_loss = nn.BCELoss()
    syncnet_wt  = hparams.syncnet_wt
 
    arr_disc_fake_loss  = []
    arr_disc_real_loss  = []
    arr_perceptual_loss = []
    # loss_fn_vgg = nn.DataParallel(LPIPS(net='vgg').to(device).eval()).to(device)
    loss_fn_vgg = LPIPS(net='alex').to(device).eval().requires_grad_(False)
    while global_epoch < nepochs:
        try:
            stop_training = False
            # print('Starting Epoch: {}'.format(global_epoch))

            running_sync_loss, running_l1_loss, running_perceptual_loss = 0., 0., 0.
            running_disc_real_loss, running_disc_fake_loss              = 0., 0.
            running_vgg_loss= 0.
            st              = time.time()
            offset          = 0
            for step, (x, indiv_mels, mel, gt, vidname) in enumerate(train_data_loader):
                load_time   = time.time() - st
                st          = time.time()
                disc.train()
                model.train()
                # print("x shape:", x.shape)
                x           = x.to(device)
                mel         = mel.to(device)
                indiv_mels  = indiv_mels.to(device)
                gt          = gt.to(device)
                
                optimizer.zero_grad()
                disc_optimizer.zero_grad()
                
                with torch.cuda.amp.autocast(enabled=False):
                    g = model(indiv_mels, x)
                    
                    if global_step > disc_iter_start:
                        fake_output     = disc(g)
                        perceptual_loss = -torch.mean(fake_output)
                    else:
                        perceptual_loss = torch.tensor(0.)
                    
                    l1loss      = recon_loss(g, gt)
                    vgg_loss    = loss_fn_vgg(torch.cat([g[:, :, i] for i in range(g.size(2))], dim=0),
                                            torch.cat([gt[:, :, i] for i in range(gt.size(2))], dim=0))
                    vgg_loss    = vgg_loss.mean()
                    nll_loss    = l1loss + vgg_loss
                    
                    # print(g.shape)
                    if global_step > sync_iter_start and syncnet_wt > 0. and syncnet is not None:
                        sync_loss = get_sync_loss(mel, g, syncnet)
                    else:
                        sync_loss = torch.tensor(0.)

                    if global_step > disc_iter_start:
                    
                        d_weight = 0.025

                    else:
                        d_weight = 0.

                    loss = syncnet_wt * sync_loss + d_weight * perceptual_loss + nll_loss
                loss.backward()
                optimizer.step()

                ### Remove all gradients before Training disc
                disc_optimizer.zero_grad()

                if global_step > disc_iter_start:
                    real_output = disc(gt)
                    fake_output = disc(g.detach())
                    disc_real_loss, disc_fake_loss  = hinge_d_loss(real_output, fake_output)
                    d_loss = 0.5 * (disc_fake_loss + disc_real_loss)
                    d_loss.backward()
                    disc_optimizer.step()
                    
                else:
                    disc_real_loss = torch.tensor(0.)
                    disc_fake_loss = torch.tensor(0.)
                

                running_disc_real_loss += disc_real_loss.item()
                arr_disc_real_loss.append(running_disc_real_loss/(step+1-offset))
                running_disc_fake_loss += disc_fake_loss.item()
                arr_disc_fake_loss.append(running_disc_fake_loss/(step+1-offset))
                
                
                # Logs
                global_step += 1
                cur_session_steps = global_step - resumed_step

                running_l1_loss += l1loss.item()
                
                if global_step > sync_iter_start and syncnet_wt > 0. and syncnet is not None:
                    running_sync_loss += sync_loss.item()
                else:
                    running_sync_loss += torch.tensor(0.)

                if hparams.disc_wt > 0.:
                    running_perceptual_loss += perceptual_loss.item()
                else:
                    running_perceptual_loss += torch.tensor(0.)

                running_vgg_loss += vgg_loss.item()
                arr_perceptual_loss.append(running_perceptual_loss/(step+1-offset))
                # logs
                if global_step == 1 or global_step % log_interval == 0:
                    logger.log_metrics({
                        "Generator/l1_loss/train": running_l1_loss/(step+1-offset),
                        "syncnet_wt": syncnet_wt,
                        "Generator/sync_loss/train": running_sync_loss/(step+1-offset),
                        "Generator/perceptual_loss/train": running_perceptual_loss/(step+1-offset),
                        "Discriminator/fake_loss/train": running_disc_fake_loss/(step+1-offset),
                        "Discriminator/real_loss/train": running_disc_real_loss/(step+1-offset)
                    }, step=global_step)
                    logger.save()

                if global_step % checkpoint_interval == 0:
                    save_checkpoint(
                        model, optimizer, global_step, checkpoint_dir, global_epoch, prefix="gen_")
                    save_checkpoint(disc, disc_optimizer, global_step, checkpoint_dir, global_epoch, prefix='disc_')
                    g = torch.clamp_(g, -1, 1)
                    save_sample_images(x, g, gt, vidname, global_step, checkpoint_dir)
                
                del x, g, gt, indiv_mels, mel
                
                
                train_time = time.time() - st
                
                print('Step {} | L1: {:.4} | Vgg: {:.4} | SW: {:.4} | Sync: {:.4} | DW: {:.4} | Percep: {:.4} | Fake: {:.4}, Real: {:.4} | Load: {:.4}, Train: {:.4}'
                        .format(global_step,
                                running_l1_loss / (step + 1-offset),
                                running_vgg_loss / (step + 1-offset),
                                syncnet_wt,
                                running_sync_loss / (step + 1-offset),
                                d_weight,
                                running_perceptual_loss / (step + 1-offset),
                                running_disc_fake_loss / (step + 1-offset),
                                running_disc_real_loss / (step + 1-offset),
                                load_time, train_time))
                st = time.time()

                if syncnet_wt > 0. and global_step > sync_iter_start and global_step % hparams.eval_interval == 0:
                    with torch.no_grad():
                        average_loss = eval_model(test_data_loader, device, model, disc, syncnet)
                        
                        logging.warning("Average loss: {}".format(average_loss))
                        
                        save_checkpoint(
                            model, optimizer, global_step, checkpoint_dir, global_epoch, prefix="gen_", loss_val=average_loss)
                        save_checkpoint(disc, disc_optimizer, global_step, checkpoint_dir, global_epoch, prefix='disc_',loss_val=average_loss)

                        if average_loss <= 0.3: # stop training
                            print("Average loss is less than 0.3. Stopping training.")
                            stop_training = True
                            break
            if stop_training:
                break
            global_epoch += 1
        except Exception as e:
            print(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            # print("KeyboardInterrupt")
            break
    print("Saving models and logs...")
    save_checkpoint(model, optimizer, global_step, checkpoint_dir, global_epoch, prefix="gen_")
    save_checkpoint(disc, disc_optimizer, global_step, checkpoint_dir, global_epoch, prefix='disc_')
    logger.save()
    valLogger.save()
    # fidLogger.save()


def eval_model(test_data_loader, device, model, disc, syncnet):
    eval_steps = 20
    logging.warning('Evaluating for {} steps'.format(eval_steps))
    running_sync_loss = 0.
    count = 0
    for step, (x, indiv_mels, mel, gt, vidname) in enumerate(test_data_loader):

        model.eval()
        disc.eval()

        x = x.to(device)
        mel = mel.to(device)
        indiv_mels = indiv_mels.to(device)
        gt = gt.to(device)

        with torch.cuda.amp.autocast(enabled=False):
            g = model(indiv_mels, x)
            sync_loss = get_sync_loss(mel, g, syncnet)
              
        running_sync_loss += sync_loss.item()
        count = step + 1
        if step >= eval_steps:
            break
        logging.warning('Step {} | Sync: {:.6}'
          .format(step , (running_sync_loss) / (step + 1)))
        
    return (running_sync_loss) / (count)


def save_ckpt(model, optimizer, step, checkpoint_dir, epoch, model_name):
    checkpoint_path = join(
        checkpoint_dir, model_name)

    optimizer_state = optimizer.state_dict() if hparams.save_optimizer_state else None
    torch.save({
        "state_dict": model.state_dict(),
        "optimizer": optimizer_state,
        "global_step": step,
        "global_epoch": epoch,
        "best_loss": best_loss,
    }, checkpoint_path)
    print("Saved checkpoint:", checkpoint_path)

def save_checkpoint(model, optimizer, step, checkpoint_dir, epoch, prefix='', loss_val=1000):
    # save best.pth
    global best_loss
    if not os.path.isdir(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    date = str(datetime.datetime.now()).split(" ")[0]
    post_fix = f'checkpoint_{hparams.img_size}_{hparams.batch_size}_{global_step:09d}_{date}.pth'
    if loss_val <= best_loss:
        best_loss = loss_val
        best_name = f"{prefix}best_wav128_1e4.pth"
        save_ckpt(model, optimizer, step, checkpoint_dir, epoch, best_name)

    last_name = f"{prefix}last_wav128_1e4.pth"
    save_ckpt(model, optimizer, step, checkpoint_dir, epoch, last_name)

    save_ckpt(model, optimizer, step, checkpoint_dir, epoch, f"{prefix}{post_fix}")

    ckpt_list = os.listdir(checkpoint_dir)
    ckpt_list = [file for file in ckpt_list if prefix in file and "checkpoint_" in file]
    num_ckpts = hparams.num_checkpoints
    if len(ckpt_list) <= num_ckpts*2:
        return

    ckpt_list.sort(key=lambda x: int(x.replace(".pth", "").split("_")[-2]))
    num_elim = len(ckpt_list) - num_ckpts
    elim_ckpt = ckpt_list[:num_elim]
    for ckpt in elim_ckpt:
        ckpt_path = os.path.join(checkpoint_dir, ckpt)
        os.remove(ckpt_path)
        print("Deleted", ckpt_path)


def _load(checkpoint_path):
    if use_cuda:
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint


def load_checkpoint(path, model, optimizer, reset_optimizer=False, overwrite_global_states=True):
    global global_step
    global global_epoch

    print("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)
    if not reset_optimizer:
        optimizer_state = checkpoint["optimizer"]
        if optimizer_state is not None:
            print("Load optimizer state from {}".format(path))
            optimizer.load_state_dict(checkpoint["optimizer"])
    if overwrite_global_states:
        global_step = checkpoint["global_step"]
        global_epoch = checkpoint["global_epoch"]

    return model


def run():
    checkpoint_dir = os.path.join(args.checkpoint_dir, args.exp_num)
    if not os.path.exists(checkpoint_dir): os.makedirs(checkpoint_dir)
    
    train_dataset = Dataset('train_lrs2_remove')
    test_dataset = Dataset('val_lrs2_remove')

    hparams.set_hparam('batch_size', 4)
    hparams.set_hparam('syncnet_wt', 0.03)
    
    train_data_loader = data_utils.DataLoader(
        train_dataset, batch_size=hparams.batch_size, shuffle=True,
        num_workers=hparams.num_workers, drop_last=True)

    # TODO: uncomment this
    test_data_loader = data_utils.DataLoader(
        test_dataset, batch_size=hparams.batch_size,
        num_workers=1, drop_last=True)

    device = torch.device("cuda" if use_cuda else "cpu")

    # TODO: uncomment this
    syncnet = SyncNet().to(device)
    model = Wav2Lip().to(device)
    disc = NLayerDiscriminator().to(device)
        
    if args.syncnet_checkpoint_path is not None:
        print("Loading syncnet from checkpoint: {}".format(args.syncnet_checkpoint_path))
        
        load_checkpoint(args.syncnet_checkpoint_path, syncnet, None, reset_optimizer=True,
                                    overwrite_global_states=False)

        # syncnet = nn.DataParallel(syncnet).to(device)
        syncnet = (syncnet).to(device)
        syncnet.eval()
        
    

    optimizer = optim.Adam([p for p in model.parameters() if p.requires_grad],
                           lr=hparams.initial_learning_rate, betas=(0.5, 0.999))
    disc_optimizer = optim.Adam([p for p in disc.parameters() if p.requires_grad], lr=hparams.disc_initial_learning_rate, betas=(0.5, 0.999))
    
    
    if args.checkpoint_path is not None:
        load_checkpoint(args.checkpoint_path, model, optimizer, reset_optimizer=False)

    if args.disc_checkpoint_path is not None:
        load_checkpoint(args.disc_checkpoint_path, disc, disc_optimizer,
                                reset_optimizer=False, overwrite_global_states=False)
    
    
    # model = nn.DataParallel(model).to(device)
    model = (model).to(device)
    # disc = nn.DataParallel(disc).to(device)
    disc = (disc).to(device)
    
    if not os.path.exists(checkpoint_dir):
        os.mkdir(checkpoint_dir)

    
    train(device, model, disc, train_data_loader, test_data_loader, optimizer, disc_optimizer,
          checkpoint_dir=checkpoint_dir,
          checkpoint_interval=hparams.checkpoint_interval,
          nepochs=hparams.nepochs,
          log_interval=hparams.log_interval,
          syncnet=syncnet)

def main():
    """Assume Single Node Multi GPUs Training Only"""
    # assert torch.cuda.is_available(), "CPU training is not allowed."

    n_gpus = torch.cuda.device_count()
    run()


if __name__ == "__main__":
    main()