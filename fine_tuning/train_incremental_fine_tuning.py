import os
import sys
sys.path.append(os.path.abspath(os.path.dirname(os.getcwd())))

from dataloader_incremental_fine_tuning import IcreLoader
from torch.utils.data import Dataset, DataLoader
import argparse
from tqdm import tqdm
from model.audio_visual_model_incremental import IncreAudioVisualNet
import torch
import torch.nn as nn
from torch.nn import functional as F
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
from torch.optim.lr_scheduler import ReduceLROnPlateau, MultiStepLR
import numpy as np
from datetime import datetime
import random

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    torch.backends.cudnn.deterministic = True

def boolean_string(s):
    if s not in {'False', 'True'}:
        raise ValueError('Not a valid boolean string')
    return s == 'True'

def CE_loss(num_classes, logits, label):
    targets = F.one_hot(label, num_classes=num_classes)
    loss = -torch.mean(torch.sum(F.log_softmax(logits, dim=-1) * targets, dim=1))

    return loss

def top_1_acc(logits, target):
    top1_res = logits.argmax(dim=1)
    top1_acc = torch.eq(target, top1_res).sum().float() / len(target)
    return top1_acc.item()

def adjust_learning_rate(args, optimizer, epoch):
    miles_list = np.array(args.milestones) - 1
    if epoch in miles_list:
        current_lr = optimizer.param_groups[0]['lr']
        new_lr = current_lr * 0.1
        print('Reduce lr from {} to {}'.format(current_lr, new_lr))
        for param_group in optimizer.param_groups: 
            param_group['lr'] = new_lr

def train(args, step, train_data_set, val_data_set):
    train_loader = DataLoader(train_data_set, batch_size=args.train_batch_size, num_workers=args.num_workers,
                              pin_memory=True, drop_last=True, shuffle=True)
    val_loader = DataLoader(val_data_set, batch_size=args.infer_batch_size, num_workers=args.num_workers,
                            pin_memory=True, drop_last=False, shuffle=False)
    
    step_out_class_num = (step + 1) * args.class_num_per_step

    if step == 0 or args.upper_bound:
        model = IncreAudioVisualNet(args, step_out_class_num)
    else:
        model = torch.load('./save/{}/{}/step_{}_best_{}_model.pkl'.format(args.dataset, args.modality, step-1, args.modality))
        model.incremental_classifier(step_out_class_num)

    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)
    
    model = model.to(device)

    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    train_loss_list = []
    val_acc_list = []
    best_val_res = 0.0
    for epoch in range(args.max_epoches):
        train_loss = 0.0
        num_steps = 0
        model.train()
        for data, labels in tqdm(train_loader):
            labels = labels.to(device)
            if args.modality == 'visual':
                visual = data
                visual = visual.to(device)
                out = model(visual=visual)
            elif args.modality == 'audio':
                audio = data
                audio = audio.to(device)
                out = model(audio=audio)
            else:
                visual = data[0]
                audio = data[1]
                visual = visual.to(device)
                audio = audio.to(device)
                out = model(visual=visual, audio=audio)

            loss = CE_loss(step_out_class_num, out, labels)
            
            model.zero_grad()
            loss.backward()
            opt.step()
            train_loss += loss.item()

            num_steps += 1
        train_loss /= num_steps
        train_loss_list.append(train_loss)
        print('Epoch:{} train_loss:{:.5f}'.format(epoch, train_loss), flush=True)

        all_val_out_logits = torch.Tensor([])
        all_val_labels = torch.Tensor([])
        model.eval()
        with torch.no_grad():
            for val_data, val_labels in tqdm(val_loader):
                # val_labels = val_labels.to(device)
                if args.modality == 'visual':
                    val_visual = val_data
                    val_visual = val_visual.to(device)
                    val_out_logits = model(visual=val_visual)
                elif args.modality == 'audio':
                    val_audio = val_data
                    val_audio = val_audio.to(device)
                    val_out_logits = model(audio=val_audio)
                else:
                    val_visual = val_data[0]
                    val_audio = val_data[1]
                    val_visual = val_visual.to(device)
                    val_audio = val_audio.to(device)
                    val_out_logits = model(visual=val_visual, audio=val_audio)
                val_out_logits = F.softmax(val_out_logits, dim=-1).detach().cpu()
                all_val_out_logits = torch.cat((all_val_out_logits, val_out_logits), dim=0)
                all_val_labels = torch.cat((all_val_labels, val_labels), dim=0)
        val_top1 = top_1_acc(all_val_out_logits, all_val_labels)
        val_acc_list.append(val_top1)
        print('Epoch:{} val_res:{:.6f} '.format(epoch, val_top1), flush=True)

        if val_top1 > best_val_res:
            best_val_res = val_top1
            print('Saving best model at Epoch {}'.format(epoch), flush=True)
            model_save_path = './save/{}/{}/step_{}_best_{}_model.pkl'.format(args.dataset, args.modality, step, args.modality)

            if torch.cuda.device_count() > 1: 
                torch.save(model.module, model_save_path)
            else:
                torch.save(model, model_save_path)
        
        plt.figure()
        plt.plot(range(len(train_loss_list)), train_loss_list, label='train_loss')
        plt.legend()
        plt.savefig('./save/fig/{}/{}/{}_train_loss_step_{}.png'.format(args.dataset, args.modality, args.modality, step))
        plt.close()

        plt.figure()
        plt.plot(range(len(val_acc_list)), val_acc_list, label='val_acc')
        plt.legend()
        plt.savefig('./save/fig/{}/{}/{}_val_acc_step_{}.png'.format(args.dataset, args.modality, args.modality, step))
        plt.close()

        if args.lr_decay:
            adjust_learning_rate(args, opt, epoch)

def detailed_test(args, step, test_data_set, task_best_acc_list):
    print("=====================================")
    print("Start testing...")
    print("=====================================")

    model_path = './save/{}/{}/step_{}_best_{}_model.pkl'.format(args.dataset, args.modality, step, args.modality)
    model = torch.load(model_path)
    
    model.to(device)

    test_loader = DataLoader(test_data_set, batch_size=args.infer_batch_size, num_workers=args.num_workers,
                             pin_memory=True, drop_last=False, shuffle=False)
    
    all_test_out_logits = torch.Tensor([])
    all_test_labels = torch.Tensor([])
    model.eval()
    with torch.no_grad():
        for test_data, test_labels in tqdm(test_loader):
            # test_labels = test_labels.to(device)
            if args.modality == 'visual':
                test_visual = test_data
                test_visual = test_visual.to(device)
                test_out_logits = model(visual=test_visual)
            elif args.modality == 'audio':
                test_audio = test_data
                test_audio = test_audio.to(device)
                test_out_logits = model(audio=test_audio)
            else:
                test_visual = test_data[0]
                test_audio = test_data[1]
                test_visual = test_visual.to(device)
                test_audio = test_audio.to(device)
                test_out_logits = model(visual=test_visual, audio=test_audio)
            test_out_logits = F.softmax(test_out_logits, dim=-1).detach().cpu()
            all_test_out_logits = torch.cat((all_test_out_logits, test_out_logits), dim=0)
            all_test_labels = torch.cat((all_test_labels, test_labels), dim=0)
    test_top1 = top_1_acc(all_test_out_logits, all_test_labels)
    print("Incremental step {} Testing res: {:.6f}".format(step, test_top1))

    if args.upper_bound:
        return None
    
    old_task_acc_list = []
    for i in range(step+1):
        step_class_list = range(i*args.class_num_per_step, (i+1)*args.class_num_per_step)
        step_class_idxs = []
        for c in step_class_list:
            idxs = np.where(all_test_labels.numpy() == c)[0].tolist()
            step_class_idxs += idxs
        step_class_idxs = np.array(step_class_idxs)
        i_labels = torch.Tensor(all_test_labels.numpy()[step_class_idxs])
        i_logits = torch.Tensor(all_test_out_logits.numpy()[step_class_idxs])
        i_acc = top_1_acc(i_logits, i_labels)
        if i == step:
            curren_step_acc = i_acc
        else:
            old_task_acc_list.append(i_acc)
    if step > 0:
        forgetting = np.mean(np.array(task_best_acc_list) - np.array(old_task_acc_list))
        print('forgetting: {:.6f}'.format(forgetting))
        for i in range(len(task_best_acc_list)):
            task_best_acc_list[i] = max(task_best_acc_list[i], old_task_acc_list[i])
    else:
        forgetting = None
    task_best_acc_list.append(curren_step_acc)

    return forgetting


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='AVE', choices=['AVE', 'ksounds', 'VGGSound_100'])
    parser.add_argument('--modality', type=str, default='visual', choices=['visual', 'audio', 'audio-visual'])
    parser.add_argument('--train_batch_size', type=int, default=64)
    parser.add_argument('--infer_batch_size', type=int, default=32)
    parser.add_argument('--num_workers', type=int, default=0)
    parser.add_argument('--max_epoches', type=int, default=500)
    parser.add_argument('--num_classes', type=int, default=28)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--weight_decay', type=float, default=1e-4)
    parser.add_argument('--lr_decay', type=boolean_string, default=False)
    parser.add_argument("--milestones", type=int, default=[500], nargs='+', help="")
    parser.add_argument('--seed', type=int, default=42)

    parser.add_argument('--class_num_per_step', type=int, default=7)
    parser.add_argument('--upper_bound', type=boolean_string, default=False)

    args = parser.parse_args()
    print(args)

    total_incremental_steps = args.num_classes // args.class_num_per_step

    setup_seed(args.seed)
    
    print('Training start time: {}'.format(datetime.now()))

    train_set = IcreLoader(args=args, mode='train', modality=args.modality)
    val_set = IcreLoader(args=args, mode='val', modality=args.modality)
    test_set = IcreLoader(args=args, mode='test', modality=args.modality)

    task_best_acc_list = []

    step_forgetting_list = []

    ckpts_root = './save/{}/{}/'.format(args.dataset, args.modality)
    figs_root = './save/fig/{}/{}/'.format(args.dataset, args.modality)

    if not os.path.exists(ckpts_root):
        os.makedirs(ckpts_root)
    if not os.path.exists(figs_root):
        os.makedirs(figs_root)

    for step in range(total_incremental_steps):
        train_set.set_incremental_step(step)
        val_set.set_incremental_step(step)
        test_set.set_incremental_step(step)

        print('Incremental step: {}'.format(step))
        train(args, step, train_set, val_set)

        step_forgetting = detailed_test(args, step, test_set, task_best_acc_list)
        if step_forgetting is not None:
            step_forgetting_list.append(step_forgetting)

    if not args.upper_bound:       
        Mean_forgetting = np.mean(step_forgetting_list)
        print('Average Forgetting: {:.6f}'.format(Mean_forgetting))

    if args.dataset != 'AVE' and args.modality != 'audio':
        train_set.close_visual_features_h5()
        val_set.close_visual_features_h5()
        test_set.close_visual_features_h5()


