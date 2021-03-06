""" Training augmented model """
import os
from math import sqrt

import numpy as np
import torch
import torch.nn as nn

import utils
from bert_fineturn.data_processor.glue import glue_compute_metrics as compute_metrics
from config import AugmentConfig
from dist_util_torch import init_gpu_params, FileLogger
from kdTool import Emd_Evaluator, distillation_loss
from modeling import TinyBertForSequenceClassification
from models.augment_cnn import AugmentCNN

acc_tasks = ["mnli", "mrpc", "sst-2", "qqp", "qnli", "rte", "books"]
corr_tasks = ["sts-b"]
mcc_tasks = ["cola"]
os.environ['CUDA_VISIBLE_DEVICES'] = '7'
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
from transformers import RobertaForSequenceClassification, GPT2ForSequenceClassification


def convert_to_attn(hidns, mask):
    if type(hidns[0]) is not tuple:
        hdim = hidns[0].shape[-1]
        attns = [torch.matmul(x, x.transpose(2, 1)) / sqrt(hdim) for x in hidns]
        mask = mask.unsqueeze(1)
        mask = (1.0 - mask) * -10000.0
        attns = [torch.nn.functional.softmax(x + mask, dim=-1) for x in attns]
    else:
        hidns = [torch.stack(x, dim=1) for x in hidns]
        hdim = hidns[0][0].shape[-1]
        attns = [torch.matmul(x, x.transpose(-1, -2)) / sqrt(hdim) for x in hidns]
        mask = mask.unsqueeze(1).unsqueeze(2)
        mask = (1.0 - mask) * -10000.0
        attns = [torch.nn.functional.softmax(x + mask, dim=-1) for x in attns]
    return attns

def main():
    config = AugmentConfig()

    init_gpu_params(config)
    logger = FileLogger('./log', config.is_master, config.is_master)

    use_emd = config.use_emd
    
    # set seed
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    torch.cuda.manual_seed_all(config.seed)

    torch.backends.cudnn.benchmark = True



    ############# LOADING DATA /START ###############
    task_name = config.datasets
    train_dataloader, _, eval_dataloader, output_mode, n_classes, config = utils.load_glue_dataset(config)
    logger.info(f"train_loader length {len(train_dataloader)}")

    ############# LOADING DATA /END ###############

    ############### BUILDING MODEL /START ###############

    model = AugmentCNN(config, n_classes, output_mode, auxiliary=False)
    pre_d, new_d = {}, {}
    for k, v in model.named_parameters():
        pre_d[k] = torch.sum(v)
    if config.teacher_type == 'gpt2':
        utils.load_gpt2_embedding_weight(model,
                                    config.teacher_model)
    elif config.teacher_type == 'bert':
        utils.load_bert_embedding_weight(model,
                                    config.teacher_model)
    elif config.teacher_type == 'roberta':
        utils.load_roberta_embedding_weight(model,
                                         config.teacher_model)
    for k, v in model.named_parameters():
        new_d[k] = torch.sum(v)
        
    logger.info("=" * 10 + "alter" + "=" * 10)
    for k in pre_d.keys():
        if pre_d[k] != new_d[k]:
            logger.info(k)
    del pre_d, new_d

    model = model.to(device)
    emd_tool = None
    if config.use_emd and config.use_kd:
        emd_tool = Emd_Evaluator(config.layers, 12, device)

    if not config.use_kd:
        teacher_model = None
    else:

        if config.teacher_type == 'gpt2':
            teacher_model = GPT2ForSequenceClassification.from_pretrained(config.teacher_model, num_labels=n_classes)
        elif config.teacher_type == 'bert':
            teacher_model = TinyBertForSequenceClassification.from_pretrained(config.teacher_model,num_labels=n_classes)
        elif config.teacher_type == 'roberta':
            teacher_model = RobertaForSequenceClassification.from_pretrained(config.teacher_model)
        teacher_model = teacher_model.to(device)
        teacher_model.eval()

    # model size
    mb_params = utils.param_size(model)
    if config.is_master:
        logger.info("Model size = {:.3f} MB".format(mb_params))

    optimizer = torch.optim.SGD(
        model.parameters(), config.lr, momentum=config.momentum, weight_decay=config.weight_decay)

    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, config.epochs/2, eta_min=config.lr_min)
    torch.autograd.set_detect_anomaly(True)
    best_top1 = 0

    ############### BUILDING MODEL /END ###############

    ############### TRAIN /START ###############
    # training loop
    for epoch in range(config.epochs):
        drop_prob = config.drop_path_prob * epoch / config.epochs
        model.drop_path_prob(drop_prob)

        # training
        train(logger, config, train_dataloader, model, teacher_model, optimizer, epoch, task_name.lower(), emd_tool=emd_tool)

        lr_scheduler.step()
        # validation
        cur_step = (epoch + 1) * len(train_dataloader)
        top1 = validate(logger, config, eval_dataloader, model, epoch, cur_step, task_name.lower(), "val")
        # top1 = validate(test_loader, model, criterion, epoch, cur_step, "test", len(task_types))
        # save
        if best_top1 < top1:
            best_top1 = top1
            is_best = True
        else:
            is_best = False
        utils.save_checkpoint(model, config.path, is_best)
        logger.info("Present best Prec@1 = {:.4%}".format(best_top1))
        print("")

    logger.info("Final best Prec@1 = {:.4%}".format(best_top1))
    ############### TRAIN /END ###############


def train(logger, config, train_loader, model, teacher_model, optimizer, epoch, task_name, emd_tool=None):
    top1 = utils.AverageMeter()
    losses = utils.AverageMeter()

    total_num_step = len(train_loader)
    cur_step = epoch * len(train_loader)
    cur_lr = optimizer.param_groups[0]['lr']

    logger.info("Epoch {} LR {}".format(epoch, cur_lr))
    # writer.add_scalar('train/lr', cur_lr, cur_step)
    model.train()

    if model.output_mode == "classification":
        criterion = nn.CrossEntropyLoss()
    elif model.output_mode == "regression":
        criterion = nn.MSELoss()

    for step, data in enumerate(train_loader):
        data = [x.to("cuda", non_blocking=True) for x in data]
        input_ids, input_mask, segment_ids, label_ids, seq_lengths = data
#       [32,128]    [32,128]   [32,128]      [32]         [32]
        X = [input_ids, input_mask, segment_ids, seq_lengths]
        y = label_ids

        N = X[0].size(0)

        optimizer.zero_grad()
        logits = model(X)  #[32,2] , [[32,128,768],[32,128,768],[32,128,768],[32,128,768]]
        if config.use_emd:
            logits, s_layer_out = logits
        if config.use_kd:
            # with torch.no_grad():
            #     check_ids = input_ids.cpu()  #[32,128]
            #     check_seg = segment_ids.cpu() #[32,128]
            #     mask_check = input_mask.cpu() #[32,128]

            if config.teacher_type == 'roberta' or config.teacher_type == 'gpt2':
                output_dict = teacher_model(input_ids, attention_mask=input_mask, output_hidden_states=True,return_dict=True)

                teacher_logits, teacher_reps = output_dict.logits,output_dict.hidden_states
            elif config.teacher_type == 'bert':
                teacher_logits, teacher_reps = teacher_model(input_ids, segment_ids, input_mask)
            # print(np.argmax(teacher_logits.detach().cpu().numpy(),axis=1))
            # print(label_ids.cpu().numpy())
            # print("#####################################################")
            kd_loss, _, _ = distillation_loss(logits, y, teacher_logits, model.output_mode, alpha=config.kd_alpha)
            rep_loss = 0
            if config.use_emd:
                if config.hidn2attn:
                    s_layer_out = convert_to_attn(s_layer_out, input_mask)
                    teacher_reps = convert_to_attn(teacher_reps, input_mask)
                rep_loss, flow, distance = emd_tool.loss(s_layer_out, teacher_reps, return_distance=True)
                if config.update_emd:
                    emd_tool.update_weight(flow, distance)
            # loss = kd_loss * config.emd_only + rep_loss * config.emd_rate
            loss = kd_loss
        else:
            loss = criterion(logits, y)
        loss.backward()

        nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
        optimizer.step()

        preds = logits.detach().cpu().numpy()
        if model.output_mode == "classification":
            preds = np.argmax(preds, axis=1)
        elif model.output_mode == "regression":
            preds = np.squeeze(preds)        # top5.update(prec5.item(), N)
        result = compute_metrics(task_name, preds, y.detach().cpu().numpy())

        if task_name == "cola":
            train_acc = result['mcc']
        elif task_name in ["sst-2", "mnli", "mnli-mm", "qnli", "rte", 'books']:
            train_acc = result['acc']
        elif task_name in ["mrpc", "qqp"]:
            train_acc = result['f1']
        elif task_name == "sts-b":
            train_acc = result['corr']
        losses.update(loss.item(), N)
        top1.update(train_acc, N)

        if step % config.print_freq == 0 or step == total_num_step - 1:
            logger.info(
                "Train: , [{:2d}/{}] Step {:03d}/{:03d} Loss {:.3f} Prec {top1:.1%}"
                .format(epoch + 1, config.epochs, step, total_num_step - 1, losses.avg, top1=train_acc))
        cur_step += 1

def validate(logger, config, data_loader, model, epoch, cur_step, task_name, mode="dev"):
    top1 = utils.AverageMeter()
    losses = utils.AverageMeter()
    if model.output_mode == "classification":
        criterion = nn.CrossEntropyLoss()
    elif model.output_mode == "regression":
        criterion = nn.MSELoss()
    eval_labels = []
    model.eval()
    preds = []
    with torch.no_grad():
        for step, data in enumerate(data_loader):
            data = [x.to(device, non_blocking=True) for x in data]
            # input_ids, input_mask, segment_ids, label_ids, seq_lengths = data
            input_ids, input_mask, segment_ids, label_ids, seq_lengths = data

            X = [input_ids, input_mask, segment_ids, seq_lengths]
            y = label_ids
            N = X[0].size(0)
            logits = model(X)
            if config.use_emd:
                logits, _ = logits
            loss = criterion(logits, y)
            correct = torch.sum(torch.argmax(logits, axis=1) == y)

            if len(preds) == 0:
                preds.append(logits.detach().cpu().numpy())
            else:
                preds[0] = np.append(preds[0], logits.detach().cpu().numpy(), axis=0)
            eval_labels.extend(y.detach().cpu().numpy())

        preds = preds[0]
        if model.output_mode == "classification":
            preds = np.argmax(preds, axis=1)
        elif model.output_mode == "regression":
            preds = np.squeeze(preds)
        result = compute_metrics(task_name, preds, eval_labels)
        print(np.sum(preds == eval_labels), len(eval_labels), result)
        if task_name == "cola":
            acc = result['mcc']
        elif task_name in ["sst-2", "mnli", "mnli-mm", "qnli", "rte", 'books']:
            acc = result['acc']
        elif task_name in ["mrpc", "qqp"]:
            acc = result['f1']
        elif task_name == "sts-b":
            acc = result['corr']

    logger.info(mode + ": [{:2d}/{}] Final Prec@1 {:.4%}".format(epoch + 1, config.epochs, acc))
    return acc


if __name__ == "__main__":
    main()
