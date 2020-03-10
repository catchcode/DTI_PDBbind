import argparse
import random
import numpy as np
import torch
import torch.nn as nn

from torch.utils.tensorboard import SummaryWriter
import os
import time
import pickle
from sklearn.metrics import r2_score, roc_auc_score
from scipy import stats
import utils
import model 

import arguments
import sys

args = arguments.parser(sys.argv)

def run(model, data_iter, data_iter2, data_iter3, train_mode):
    model.train() if train_mode else model.eval
    losses, losses_der1, losses_der2, losses_docking, losses_screening = \
            [], [], [], [], []
    
    save_pred, save_true, save_pred_docking, save_true_docking, \
            save_pred_screening, save_true_screening = \
            dict(), dict(), dict(), dict(), dict(), dict()
    i_batch = 0
    while True:
        model.zero_grad()
        sample = next(data_iter, None)
        if sample is None: break
        sample = utils.dic_to_device(sample, device)
        keys, affinity = sample['key'], sample['affinity']

        loss_all = 0.0
        cal_der_loss = False
        if args.loss_der1_ratio>0.0 or args.loss_der2_ratio>0.0: 
            cal_der_loss=True
        pred, loss_der1, loss_der2 = model(sample, cal_der_loss=True)
        loss = loss_fn(pred.sum(-1), affinity)
        loss_der2 = loss_der2.clamp(min=args.min_loss_der2)
        loss_all += loss
        loss_all += loss_der1*args.loss_der1_ratio
        loss_all += loss_der2*args.loss_der2_ratio
        
        #loss4
        loss_docking = torch.zeros((1,))
        keys_docking = []
        if args.loss_docking_ratio>0.0:
            sample_docking = next(data_iter2, None)
            sample_docking = utils.dic_to_device(sample_docking, device)
            keys_docking, affinity_docking = \
                    sample_docking['key'], sample_docking['affinity']
            pred_docking, _, _ = model(sample_docking)
            loss_docking = (affinity_docking-pred_docking.sum(-1))
            loss_docking = loss_docking.clamp(args.min_loss_docking).mean()
            loss_all+=loss_docking*args.loss_docking_ratio

        loss_screening = torch.zeros((1,))
        keys_screening = []
        if args.loss_screening_ratio>0.0:
            sample_screening = next(data_iter3, None)
            sample_screening = utils.dic_to_device(sample_screening, device)
            keys_screening, affinity_screening = \
                    sample_screening['key'], sample_screening['affinity']
            pred_screening, _, _ = model(sample_screening)
            loss_screening = (affinity_screening-pred_screening.sum(-1)).clamp(min=0.0).mean()
            loss_all+=loss_screening*args.loss_screening_ratio
        
        if train_mode:
            loss_all.backward()
            optimizer.step()
        losses.append(loss.data.cpu().numpy())
        losses_der1.append(loss_der1.data.cpu().numpy())
        losses_der2.append(loss_der2.data.cpu().numpy())
        losses_docking.append(loss_docking.data.cpu().numpy())
        losses_screening.append(loss_screening.data.cpu().numpy())
        affinity = affinity.data.cpu().numpy()
        pred = pred.data.cpu().numpy()
        for i in range(len(keys)):
            save_pred[keys[i]] = pred[i]
            save_true[keys[i]] = affinity[i]
        
        if len(keys_docking)>0:
            pred_docking = pred_docking.data.cpu().numpy()
            for i in range(len(keys_docking)):
                save_pred_docking[keys_docking[i]] = pred_docking[i]
                save_true_docking[keys_docking[i]] = affinity_docking[i]
        
        if len(keys_screening)>0:
            pred_screening = pred_screening.data.cpu().numpy()
            for i in range(len(keys_screening)):
                save_pred_screening[keys_screening[i]] = pred_screening[i]
                save_true_screening[keys_screening[i]] = affinity_screening[i]
        
        i_batch+=1

    losses = np.mean(np.array(losses))
    losses_der1 = np.mean(np.array(losses_der1))
    losses_der2 = np.mean(np.array(losses_der2))
    losses_docking = np.mean(np.array(losses_docking))
    losses_screening = np.mean(np.array(losses_screening))
    return losses, losses_der1, losses_der2, losses_docking, losses_screening, \
            save_pred, save_true, save_pred_docking, save_true_docking, \
            save_pred_screening, save_true_screening \

#Make directory for save files
os.makedirs(args.save_dir, exist_ok=True)
os.makedirs(args.tensorboard_dir, exist_ok=True)

#Set GPU
cmd = utils.set_cuda_visible_device(args.ngpu)
os.environ['CUDA_VISIBLE_DEVICES']=cmd[:-1]

#Read labels
train_keys, test_keys, id_to_y = utils.read_data(args.filename, args.key_dir)
train_keys2, test_keys2, id_to_y2 = utils.read_data(args.filename2, args.key_dir2)
train_keys3, test_keys3, id_to_y3 = utils.read_data(args.filename3, args.key_dir3)

#Model
if args.potential=='morse': model = model.DTIMorse(args)
elif args.potential=='morse_all_pair': model = model.DTIMorseAllPair(args)
elif args.potential=='harmonic': model = model.DTIHarmonic(args)
else: 
    print (f'No {args.potential} potential')
    exit(-1)

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
model = utils.initialize_model(model, device, args.restart_file)

print ('number of parameters : ', sum(p.numel() for p in model.parameters() 
                    if p.requires_grad))

#Dataloader

train_dataset, train_dataloader, test_dataset, test_dataloader = \
        utils.get_dataset_dataloader(train_keys, test_keys, args.data_dir, 
                id_to_y, args.batch_size, args.num_workers, args.pos_noise_std)
train_dataset2, train_dataloader2, test_dataset2, test_dataloader2 = \
        utils.get_dataset_dataloader(train_keys2, test_keys2, args.data_dir2, 
                id_to_y2, args.batch_size, args.num_workers, args.pos_noise_std)
train_dataset3, train_dataloader3, test_dataset3, test_dataloader3 = \
        utils.get_dataset_dataloader(train_keys3, test_keys3, args.data_dir3, 
                id_to_y3, args.batch_size, args.num_workers, args.pos_noise_std)

#Optimizer and loss
optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, \
                             weight_decay=args.weight_decay)
loss_fn = nn.MSELoss()

#train
writer = SummaryWriter(args.tensorboard_dir)
for epoch in range(args.num_epochs):
    st = time.time()
    tmp_st = st

    train_losses, train_losses_der1, train_losses_der2, train_losses_docking, \
            train_losses_screening = [], [], [], [], []
    test_losses, test_losses_der1, test_losses_der2, test_losses_docking, \
            test_losses_screening = [], [], [], [], []
    
    train_pred, train_true, train_pred_docking, train_true_docking, \
            train_pred_screening, train_true_screening = \
            dict(), dict(), dict(), dict(), dict(), dict()
    test_pred, test_true, test_pred_docking, test_true_docking, \
            test_pred_screening, test_true_screening = \
            dict(), dict(), dict(), dict(), dict(), dict()

    #iterator
    train_data_iter, train_data_iter2, train_data_iter3 = \
            iter(train_dataloader), iter(train_dataloader2), iter(train_dataloader3)
    test_data_iter, test_data_iter2, test_data_iter3 = \
            iter(test_dataloader), iter(test_dataloader2), iter(test_dataloader3)

    #Train
    train_losses, train_losses_der1, train_losses_der2, \
            train_losses_docking, train_losses_screening, \
            train_pred, train_true, train_pred_docking, train_true_docking, \
            train_pred_screening, train_true_screening = \
            run(model, train_data_iter, train_data_iter2, train_data_iter3, True)
    
    #Test
    test_losses, test_losses_der1, test_losses_der2, \
            test_losses_docking, test_losses_screening, \
            test_pred, test_true, test_pred_docking, test_true_docking, \
            test_pred_screening, test_true_screening = \
            run(model, test_data_iter, test_data_iter2, test_data_iter3, False)
    #Write tensorboard
    writer.add_scalars('train',
                       {'loss': train_losses,
                        'loss_der1': train_losses_der1,
                        'loss_der2': train_losses_der2,
                        'loss_docking': train_losses_docking,
                        'loss_screening': train_losses_screening,
                        },
                       epoch)
    writer.add_scalars('test',
                       {'loss': test_losses,
                        'loss_der1': test_losses_der1,
                        'loss_der2': test_losses_der2,
                        'loss_docking': test_losses_docking,
                        'loss_screening': test_losses_screening,
                        },
                       epoch)

    #Write prediction
    utils.write_result(args.train_result_filename, train_pred, train_true)
    utils.write_result(args.test_result_filename, test_pred, test_true)
    utils.write_result(args.train_result_docking_filename, 
            train_pred_docking, train_true_docking)
    utils.write_result(args.test_result_docking_filename, 
            test_pred_docking, test_true_docking)
    utils.write_result(args.train_result_screening_filename, 
            train_pred_screening, train_true_screening)
    utils.write_result(args.test_result_screening_filename, 
            test_pred_screening, test_true_screening)
    end = time.time()
    
    #Cal R2
    train_r2 = r2_score([train_true[k] for k in train_true.keys()], \
            [train_pred[k].sum() for k in train_true.keys()])
    test_r2 = r2_score([test_true[k] for k in test_true.keys()], \
            [test_pred[k].sum() for k in test_true.keys()])

    #Cal R 
    _, _, test_r, _, _ = \
            stats.linregress([test_true[k] for k in test_true.keys()],                        
                            [test_pred[k].sum() for k in test_true.keys()])
    _, _, train_r, _, _ = \
            stats.linregress([train_true[k] for k in train_true.keys()],                        
                            [train_pred[k].sum() for k in train_true.keys()])
    end = time.time()
    if epoch==0: 
        print ("epoch\ttrain_l\ttrain_l_der1\ttrain_l_der2\ttrain_l_docking\t"+
                "train_l_screening\ttest_l\ttest_l_der1\ttest_l_der2\t"+
                "test_l_docking\ttest_l_screening\t"+
                "train_r2\ttest_r2\ttrain_r\ttest_r\ttime")
    print (f"{epoch}\t{train_losses:.3f}\t{train_losses_der1:.3f}\t"+
           f"{train_losses_der2:.3f}\t{train_losses_docking:.3f}\t"+
           f"{train_losses_screening:.3f}\t"+
           f"{test_losses:.3f}\t{test_losses_der1:.3f}\t"+
           f"{test_losses_der2:.3f}\t{test_losses_docking:.3f}\t"+
           f"{test_losses_screening:.3f}\t"+
           f"{train_r2:.3f}\t{test_r2:.3f}\t"+
           f"{train_r:.3f}\t{test_r:.3f}\t{end-st:.3f}")
    
    name = os.path.join(args.save_dir, 'save_'+str(epoch)+'.pt')
    torch.save(model.state_dict(), name)
    
    lr = args.lr * ((args.lr_decay)**epoch)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr             
