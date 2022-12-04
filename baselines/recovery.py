import logging
import numpy as np
import os
import random
import statistics
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from argparse import ArgumentParser
from torch.autograd import Variable

import utils
from classify import *
from discri import *
from evaluation import get_knn_dist, calc_fid
from generator import *
from generator import Generator
from utils import log_sum_exp, save_tensor_images


# logger
def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger


def reparameterize(mu, logvar):
    """
    Reparameterization trick to sample from N(mu, var) from
    N(0,1).
    :param mu: (Tensor) Mean of the latent Gaussian [B x D]
    :param logvar: (Tensor) Standard deviation of the latent Gaussian [B x D]
    :return: (Tensor) [B x D]
    """
    std = torch.exp(0.5 * logvar)
    eps = torch.randn_like(std)

    return eps * std + mu


def dist_inversion(G, D, T, E, iden, itr, lr=2e-2, momentum=0.9, lamda=100, iter_times=1500, clip_range=1,
                   improved=False, num_seeds=5, save_dir=''):
    save_img_dir = os.path.join(save_dir, 'all_imgs')
    success_dir = os.path.join(save_dir, 'success_imgs')
    os.makedirs(save_img_dir, exist_ok=True)
    os.makedirs(success_dir, exist_ok=True)

    iden = iden.view(-1).long().cuda()
    criterion = nn.CrossEntropyLoss().cuda()
    bs = iden.shape[0]

    G.eval()
    D.eval()
    T.eval()
    E.eval()

    no = torch.zeros(bs)  # index for saving all success attack images

    tf = time.time()

    # NOTE
    mu = Variable(torch.zeros(bs, 100), requires_grad=True)
    log_var = Variable(torch.ones(bs, 100), requires_grad=True)

    params = [mu, log_var]
    solver = optim.Adam(params, lr=lr)
    # scheduler = torch.optim.lr_scheduler.StepLR(solver, 1800, gamma=0.1)

    for i in range(iter_times):
        z = reparameterize(mu, log_var)
        fake = G(z)
        if improved == True:
            _, label = D(fake)
        else:
            label = D(fake)

        out = T(fake)[-1]

        for p in params:
            if p.grad is not None:
                p.grad.data.zero_()

        if improved:
            Prior_Loss = torch.mean(F.softplus(log_sum_exp(label))) - torch.mean(log_sum_exp(label))
            # Prior_Loss =  torch.mean(F.softplus(log_sum_exp(label))) - torch.mean(label.gather(1, iden.view(-1, 1)))  #1 class prior
        else:
            Prior_Loss = - label.mean()
        Iden_Loss = criterion(out, iden)

        Total_Loss = Prior_Loss + lamda * Iden_Loss

        Total_Loss.backward()
        solver.step()

        z = torch.clamp(z.detach(), -clip_range, clip_range).float()

        Prior_Loss_val = Prior_Loss.item()
        Iden_Loss_val = Iden_Loss.item()

        if (i + 1) % 300 == 0:
            with torch.no_grad():
                fake_img = G(z.detach())
                eval_prob = E(utils.low2high(fake_img))[-1]
                eval_iden = torch.argmax(eval_prob, dim=1).view(-1)
                acc = iden.eq(eval_iden.long()).sum().item() * 1.0 / bs
                print(
                    "Iteration:{}\tPrior Loss:{:.2f}\tIden Loss:{:.2f}\tAttack Acc:{:.2f}".format(i + 1, Prior_Loss_val,
                                                                                                  Iden_Loss_val, acc))

    interval = time.time() - tf
    print("Time:{:.2f}".format(interval))

    with torch.no_grad():
        res = []
        res5 = []
        seed_acc = torch.zeros((bs, 5))
        for random_seed in range(num_seeds):
            tf = time.time()
            z = reparameterize(mu, log_var)
            fake = G(z)
            score = T(fake)[-1]
            eval_prob = E(utils.low2high(fake))[-1]
            eval_iden = torch.argmax(eval_prob, dim=1).view(-1)

            cnt, cnt5 = 0, 0
            for i in range(bs):
                gt = iden[i].item()
                sample = fake[i]
                all_img_class_path = os.path.join(save_img_dir, str(gt))
                if not os.path.exists(all_img_class_path):
                    os.makedirs(all_img_class_path)
                save_tensor_images(sample.detach(),
                                   os.path.join(all_img_class_path, "attack_iden_{}_{}.png".format(gt, random_seed)))

                if eval_iden[i].item() == gt:
                    seed_acc[i, random_seed] = 1
                    cnt += 1
                    best_img = G(z)[i]
                    success_img_class_path = os.path.join(success_dir, str(gt))
                    if not os.path.exists(success_img_class_path):
                        os.makedirs(success_img_class_path)
                    save_tensor_images(best_img.detach(), os.path.join(success_img_class_path,
                                                                       "{}_attack_iden_{}_{}.png".format(itr, gt,
                                                                                                         int(no[i]))))
                    no[i] += 1
                _, top5_idx = torch.topk(eval_prob[i], 5)
                if gt in top5_idx:
                    cnt5 += 1

            interval = time.time() - tf
            print("Time:{:.2f}\tSeed:{}\tAcc:{:.2f}\t".format(interval, random_seed, cnt * 1.0 / bs))
            res.append(cnt * 1.0 / bs)
            res5.append(cnt5 * 1.0 / bs)

            torch.cuda.empty_cache()

    acc, acc_5 = statistics.mean(res), statistics.mean(res5)
    acc_var = statistics.variance(res)
    acc_var5 = statistics.variance(res5)
    print("Acc:{:.2f}\tAcc_5:{:.2f}\tAcc_var:{:.4f}\tAcc_var5:{:.4f}".format(acc, acc_5, acc_var, acc_var5))

    return acc, acc_5, acc_var, acc_var5


def inversion(G, D, T, E, iden, itr, lr=2e-2, momentum=0.9, lamda=100, iter_times=1500, clip_range=1, improved=False,
              num_seeds=5, save_dir=''):
    save_img_dir = os.path.join(save_dir, 'all_imgs')
    success_dir = os.path.join(save_dir, 'success_imgs')
    os.makedirs(save_img_dir, exist_ok=True)
    os.makedirs(success_dir, exist_ok=True)

    iden = iden.view(-1).long().cuda()
    criterion = nn.CrossEntropyLoss().cuda()
    bs = iden.shape[0]

    G.eval()
    D.eval()
    T.eval()
    E.eval()

    flag = torch.zeros(bs)
    no = torch.zeros(bs)  # index for saving all success attack images

    res = []
    res5 = []
    seed_acc = torch.zeros((bs, 5))
    for random_seed in range(num_seeds):
        tf = time.time()
        r_idx = random_seed
        torch.manual_seed(random_seed)
        torch.cuda.manual_seed(random_seed)
        np.random.seed(random_seed)
        random.seed(random_seed)

        z = torch.randn(bs, 100).cuda().float()
        z.requires_grad = True
        v = torch.zeros(bs, 100).cuda().float()

        for i in range(iter_times):
            fake = G(z)
            if improved == True:
                _, label = D(fake)
            else:
                label = D(fake)

            out = T(fake)[-1]

            if z.grad is not None:
                z.grad.data.zero_()

            if improved:
                Prior_Loss = torch.mean(F.softplus(log_sum_exp(label))) - torch.mean(log_sum_exp(label))
            # Prior_Loss =  torch.mean(F.softplus(log_sum_exp(label))) - torch.mean(label.gather(1, iden.view(-1, 1)))  #1 class prior
            else:
                Prior_Loss = - label.mean()

            Iden_Loss = criterion(out, iden)

            Total_Loss = Prior_Loss + lamda * Iden_Loss

            Total_Loss.backward()

            v_prev = v.clone()
            gradient = z.grad.data
            v = momentum * v - lr * gradient
            z = z + (- momentum * v_prev + (1 + momentum) * v)
            z = torch.clamp(z.detach(), -clip_range, clip_range).float()
            z.requires_grad = True

            Prior_Loss_val = Prior_Loss.item()
            Iden_Loss_val = Iden_Loss.item()

            if (i + 1) % 300 == 0:
                with torch.no_grad():
                    fake_img = G(z.detach())
                    eval_prob = E(utils.low2high(fake_img))[-1]
                    eval_iden = torch.argmax(eval_prob, dim=1).view(-1)
                    acc = iden.eq(eval_iden.long()).sum().item() * 1.0 / bs
                    print("Iteration:{}\tPrior Loss:{:.2f}\tIden Loss:{:.2f}\tAttack Acc:{:.2f}".format(i + 1,
                                                                                                        Prior_Loss_val,
                                                                                                        Iden_Loss_val,
                                                                                                        acc))

        with torch.no_grad():
            fake = G(z)
            score = T(fake)[-1]
            eval_prob = E(utils.low2high(fake))[-1]
            eval_iden = torch.argmax(eval_prob, dim=1).view(-1)

            cnt, cnt5 = 0, 0
            for i in range(bs):
                gt = iden[i].item()
                sample = G(z)[i]
                all_img_class_path = os.path.join(save_img_dir, str(gt))
                if not os.path.exists(all_img_class_path):
                    os.makedirs(all_img_class_path)
                save_tensor_images(sample.detach(),
                                   os.path.join(all_img_class_path, "attack_iden_{}_{}.png".format(gt, r_idx)))

                if eval_iden[i].item() == gt:
                    seed_acc[i, r_idx] = 1
                    cnt += 1
                    flag[i] = 1
                    best_img = G(z)[i]
                    success_img_class_path = os.path.join(success_dir, str(gt))
                    if not os.path.exists(success_img_class_path):
                        os.makedirs(success_img_class_path)
                    save_tensor_images(best_img.detach(), os.path.join(success_img_class_path,
                                                                       "{}_attack_iden_{}_{}.png".format(itr, gt,
                                                                                                         int(no[i]))))
                    no[i] += 1
                _, top5_idx = torch.topk(eval_prob[i], 5)
                if gt in top5_idx:
                    cnt5 += 1

            interval = time.time() - tf
            print("Time:{:.2f}\tAcc:{:.2f}\t".format(interval, cnt * 1.0 / bs))
            res.append(cnt * 1.0 / bs)
            res5.append(cnt5 * 1.0 / bs)
            torch.cuda.empty_cache()

    acc, acc_5 = statistics.mean(res), statistics.mean(res5)
    acc_var = statistics.variance(res)
    acc_var5 = statistics.variance(res5)
    print("Acc:{:.2f}\tAcc_5:{:.2f}\tAcc_var:{:.4f}\tAcc_var5:{:.4f}".format(acc, acc_5, acc_var, acc_var5))

    return acc, acc_5, acc_var, acc_var5


if __name__ == "__main__":
    global args, logger

    parser = ArgumentParser(description='Step2: targeted recovery')
    parser.add_argument('--model', default='VGG16', help='VGG16 | IR152 | FaceNet64')
    parser.add_argument('--improved_flag', action='store_true', default=False, help='use improved k+1 GAN')
    parser.add_argument('--dist_flag', action='store_true', default=False, help='use distributional recovery')
    parser.add_argument('--save_dir', type=str,
                        default='Inversion_Results')
    parser.add_argument('--path_G', type=str,
                        default='')
    parser.add_argument('--path_D', type=str,
                        default='')
    args = parser.parse_args()
    logger = get_logger()

    logger.info(args)
    logger.info("=> creating model ...")

    print("=> Using improved GAN:", args.improved_flag)

    z_dim = 100
    ###########################################
    ###########     load model       ##########
    ###########################################
    G = Generator(z_dim)
    G = torch.nn.DataParallel(G).cuda()
    if args.improved_flag == True:
        D = MinibatchDiscriminator()
        # path_G = './improvedGAN/improved_celeba_G.tar'
        # path_D = './improvedGAN/improved_celeba_D.tar'
        path_G = args.path_G
        path_D = args.path_D
    else:
        D = DGWGAN(3)
        # path_G = './improvedGAN/celeba_G.tar'
        # path_D = './improvedGAN/celeba_D.tar'
        path_G = args.path_G
        path_D = args.path_D

    D = torch.nn.DataParallel(D).cuda()
    ckp_G = torch.load(path_G)
    G.load_state_dict(ckp_G['state_dict'], strict=False)
    ckp_D = torch.load(path_D)
    D.load_state_dict(ckp_D['state_dict'], strict=False)

    if args.model.startswith("VGG16"):
        T = VGG16(1000)
        path_T = '../checkpoints/target_model/VGG16_88.26.tar'
    elif args.model.startswith('IR152'):
        T = IR152(1000)
        path_T = '../checkpoints/target_model/IR152_91.16.tar'
    elif args.model == "FaceNet64":
        T = FaceNet64(1000)
        path_T = '../checkpoints/target_model/FaceNet64_88.50.tar'
    print("Target Model: ", path_T)

    T = torch.nn.DataParallel(T).cuda()
    ckp_T = torch.load(path_T)
    T.load_state_dict(ckp_T['state_dict'], strict=False)

    E = FaceNet(1000)
    E = torch.nn.DataParallel(E).cuda()
    path_E = '../checkpoints/evaluate_model/FaceNet_95.88.tar'
    ckp_E = torch.load(path_E)
    E.load_state_dict(ckp_E['state_dict'], strict=False)

    ############         attack     ###########
    logger.info("=> Begin attacking ...")

    aver_acc, aver_acc5, aver_var, aver_var5 = 0, 0, 0, 0
    for i in range(1):
        iden = torch.from_numpy(np.arange(60))

        # evaluate on the first 300 identities only
        for idx in range(5):
            print("--------------------- Attack batch [%s]------------------------------" % idx)
            if args.dist_flag == True:
                acc, acc5, var, var5 = dist_inversion(G, D, T, E, iden, itr=i, lr=2e-2, momentum=0.9, lamda=100,
                                                      iter_times=1500, clip_range=1, improved=args.improved_flag,
                                                      num_seeds=5, save_dir=args.save_dir)
            else:
                acc, acc5, var, var5 = inversion(G, D, T, E, iden, itr=i, lr=2e-2, momentum=0.9, lamda=100,
                                                 iter_times=1500, clip_range=1, improved=args.improved_flag,
                                                 save_dir=args.save_dir)

            iden = iden + 60
            aver_acc += acc / 5
            aver_acc5 += acc5 / 5
            aver_var += var / 5
            aver_var5 += var5 / 5

    print("Average Acc:{:.2f}\tAverage Acc5:{:.2f}\tAverage Acc_var:{:.4f}\tAverage Acc_var5:{:.4f}".format(aver_acc,
                                                                                                            aver_acc5,
                                                                                                            aver_var,
                                                                                                            aver_var5))

    print("=> Calculate the KNN dist.")
    knn_dist = get_knn_dist(E, os.path.join(args.save_dir, 'all_imgs'), "../celeba_private_feats")
    print("KNN Dist %.2f" % knn_dist)

    print("=> Calculate the FID.")
    fid = calc_fid(recovery_img_path=os.path.join(args.save_dir, "success_imgs"),
                   private_img_path="../datasets/celeba_private_domain", batch_size=100)
    print("FID %.2f" % fid)
