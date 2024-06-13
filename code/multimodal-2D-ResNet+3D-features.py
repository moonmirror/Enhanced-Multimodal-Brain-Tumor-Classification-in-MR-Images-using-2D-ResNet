import os
import random
import itertools

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
from sklearn import metrics
from torch.utils.data import DataLoader
from torch.utils.data import Dataset
from sklearn.preprocessing import MinMaxScaler
from torchvision import transforms
from tensorboardX import SummaryWriter
from tqdm import tqdm
import pandas as pd
from PIL import Image
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")


torch.manual_seed(1020)  
torch.cuda.manual_seed_all(1020)  
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True
os.environ["CUDA_VISIBLE_DEVICES"] = '0'
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(device)


class CyclicShift(object):
    def __init__(self, p=0.5):
        print('use CyclicShift')
        self.p = p

    def __call__(self, img):
        p = np.random.random()
        if p < self.p:
            img = self.move(img)
        return img

    def move(self, img):
        w, h = img.width, img.height
        w = min(w, h)
        rate = np.random.randint(w // 4, w // 2)
        img_data = np.asarray(img)
        img_moving = np.zeros_like(img_data)
        directions = np.random.random()
        if directions < 0.25:
            img_moving[: rate] = img_data[-rate:]
            img_moving[rate:] = img_data[: -rate]
        elif directions < 0.5:
            img_moving[-rate:] = img_data[: rate]
            img_moving[: -rate] = img_data[rate:]
        elif directions < 0.75:
            img_moving[:, -rate:] = img_data[:, : rate]
            img_moving[:, : -rate] = img_data[:, rate:]
        else:
            img_moving[:, : rate] = img_data[:, -rate:]
            img_moving[:, rate:] = img_data[:, : -rate]
        return Image.fromarray(img_moving)


def add_salt_pepper_noise(img, prob):
    image = np.array(img).copy()
    output = np.zeros(image.shape, np.uint8)
    thres = 1 - prob
    for i in range(image.shape[0]):
        for j in range(image.shape[1]):
            rdn = np.random.random()
            if rdn < prob:
                output[i][j] = 0
            elif rdn > thres:
                output[i][j] = 255
            else:
                output[i][j] = image[i][j]
    return Image.fromarray(output.astype('uint8'))


class AddPepperNoise(object):
    def __init__(self, snr, p=0.5):
        self.snr = snr
        self.p = p

    def __call__(self, img):
        if random.uniform(0, 1) < self.p:
            img = add_salt_pepper_noise(img, self.snr)
        return img


train_compose = transforms.Compose([
    transforms.Resize((448, 448)),
    transforms.CenterCrop(224),
    transforms.RandomCrop(160),
    transforms.RandomHorizontalFlip(),
    transforms.RandomVerticalFlip(),
    transforms.RandomRotation((-180, 180)),
    CyclicShift(),
    AddPepperNoise(0.05),
    transforms.ToTensor(),
    transforms.Normalize((0.5, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5))
])

test_compose = transforms.Compose([
    transforms.CenterCrop(160),
    transforms.ToTensor(),
    transforms.Normalize((0.5, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5))
])


class GetLoader(Dataset):
    def __init__(self, root, type_):
        super().__init__()
        self.root = Path(root)
        self.type = type_
        if self.type == 'train':
            self.csv = self.root / "T2_train.csv"
            self.transform = train_compose
        else:
            self.csv = self.root / "T2_test.csv"
            self.transform = test_compose

        self.csv = pd.read_csv(self.csv)
        self.csv = self.csv.dropna()
        self.csv['ID'] = self.csv['ID'].astype(str)
        self.people_classify = self.csv.loc[:, 'label']
        self.people_classify.index = self.csv['ID']
        self.people_classify = self.people_classify.to_dict()

        self.four_pic = []
        self.two_pic = []
        self.pic_files = []

        for p in self.people_classify:
            pic_file = self.root / str(p)
            pic_file = list(pic_file.rglob('*.png'))

            if self.people_classify[p] == 4:
                self.four_pic += pic_file
            else:
                self.two_pic += pic_file

        if self.type == "train":
            ratio2 = int(len(self.four_pic) // len(self.two_pic))
            self.pic_files = self.two_pic + self.four_pic
            random.shuffle(self.pic_files)
        else:
            self.pic_files = self.four_pic + self.two_pic

    def __getitem__(self, index):
        img_t1_path = str(self.pic_files[index])
        img_t2_path = img_t1_path.replace('T1', 'T2')
        img_t1c_path = img_t1_path.replace('T1', 'T1c')
        img_flair_path = img_t1_path.replace('T1', 'FLAIR')

        image = np.zeros((240, 240, 4))

        img_t1 = Image.open(img_t1_path)
        img_t2 = Image.open(img_t2_path)
        img_t1c = Image.open(img_t1c_path)
        img_flair = Image.open(img_flair_path)

        image[:, :, 0] = np.array(img_t1)
        image[:, :, 1] = np.array(img_t2)
        image[:, :, 2] = np.array(img_t1c)
        image[:, :, 3] = np.array(img_flair)

        image = Image.fromarray(np.uint8(image))

        people = str(self.pic_files[index].parent.name)
        idd = str(people)
        y = self.people_classify[str(people)]
        if y == 2:
            one_hot = [0, 1]
        else:
            one_hot = [1, 0]
            if self.type == 'train':
                self.transform = test_compose

        one_hot = torch.tensor(one_hot)
        img_data = self.transform(image)

        rs = {
            "img": img_data,
            "label_pre": y-2,
            "label": one_hot,
            "id": idd,
            "image_path": str(self.pic_files[index])
        }

        return rs

    def __len__(self):
        return len(self.pic_files)


# ResNet
def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)


def conv1x1(in_planes, out_planes, stride=1):
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None):
        super(BasicBlock, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        if groups != 1 or base_width != 64:
            raise ValueError('BasicBlock only supports groups=1 and base_width=64')
        if dilation > 1:
            raise NotImplementedError("Dilation > 1 not supported in BasicBlock")

        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = norm_layer(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = norm_layer(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, groups=1, base_width=64, dilation=1,
                 norm_layer=None):
        super(Bottleneck, self).__init__()

        if norm_layer is None:
            norm_layer = nn.BatchNorm2d

        width = int(planes * (base_width / 64.)) * groups

        self.conv1 = conv1x1(inplanes, width)
        self.bn1 = norm_layer(width)
        self.conv2 = conv3x3(width, width, stride, groups, dilation)
        self.bn2 = norm_layer(width)
        self.conv3 = conv1x1(width, planes * self.expansion)
        self.bn3 = norm_layer(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride


    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            identity = self.downsample(identity)

        out += identity
        out = self.relu(out)

        return out


class ResNet(nn.Module):
    def __init__(self, block, layers, num_classes=2, zero_init_residual=False,
                 groups=1, width_per_group=64, replace_stride_with_dilation=None,
                 norm_layer=None):

        super(ResNet, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        self._norm_layer = norm_layer
        self.inplanes = 64
        self.dilation = 1

        if replace_stride_with_dilation is None:
            replace_stride_with_dilation = [False, False, False]

        if len(replace_stride_with_dilation) != 3:
            raise ValueError("replace_stride_with_dilation should be None "
                             "or a 3-element tuple, got {}".format(replace_stride_with_dilation))

        self.block = block
        self.groups = groups
        self.base_width = width_per_group

        self.conv1 = nn.Conv2d(4, self.inplanes, kernel_size=7, stride=2, padding=3, bias=False)

        self.bn1 = norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)

        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)


        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2, dilate=replace_stride_with_dilation[0])
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2, dilate=replace_stride_with_dilation[1])
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2, dilate=replace_stride_with_dilation[2])

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))


        self.fc = nn.Linear(512 * block.expansion, 14)
        self.fc2 = nn.Linear(28, num_classes)


        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)


    def _make_layer(self, block, planes, blocks, stride=1, dilate=False):
        norm_layer = self._norm_layer
        downsample = None
        previous_dilation = self.dilation

        if dilate:
            self.dilation *= stride
            stride = 1

        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, self.groups,
                            self.base_width, previous_dilation, norm_layer))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes, groups=self.groups,
                                base_width=self.base_width, dilation=self.dilation,
                                norm_layer=norm_layer))

        return nn.Sequential(*layers)


    def forward(self, x, y):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.avgpool(x)


        x = torch.flatten(x, 1)
        x = self.fc(x)
        x = torch.cat([x, y], 1)
        x = self.fc2(x)
        return x


def resnet18(**kwargs):
    model = ResNet(BasicBlock, [2, 2, 2, 2], **kwargs)
    return model


def resnet34(**kwargs):
    model = ResNet(BasicBlock, [3, 4, 6, 3], **kwargs)
    return model


def resnet50(**kwargs):
    model = ResNet(Bottleneck, [3, 4, 6, 3], **kwargs)
    return model


def resnet101(**kwargs):
    model = ResNet(Bottleneck, [3, 4, 23, 3], **kwargs)
    return model



# Confusion_matrix
def confusion_matrix(preds, labels, conf_matrix, num):
    for p, t in zip(preds, labels):
        conf_matrix[t, p] += num
    return conf_matrix


def plot_confusion_matrix(cm, classes, normalize=False, title='Confusion matrix', cmap=plt.cm.Blues):
    fig = plt.figure(figsize=None)
    plt.imshow(cm, interpolation='nearest', cmap=cmap)
    plt.title(title)
    plt.colorbar()
    tick_marks = np.arange(len(classes))
    plt.xticks(tick_marks, classes, rotation=90)
    plt.yticks(tick_marks, classes)

    plt.axis("equal")
    ax = plt.gca()  
    left, right = plt.xlim()  
    ax.spines['left'].set_position(('data', left))
    ax.spines['right'].set_position(('data', right))
    for edge_i in ['top', 'bottom', 'right', 'left']:
        ax.spines[edge_i].set_edgecolor("white")

    thresh = cm.max() / 2.
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        num = '{:.2f}'.format(cm[i, j]) if normalize else int(cm[i, j])
        plt.text(j, i, num,
                 verticalalignment='center',
                 horizontalalignment="center",
                 color="white" if num > thresh else "black")
    plt.tight_layout()
    plt.ylabel('True label')
    plt.xlabel('Predicted label')
    return fig


def statis_auc(ytrue, ypred):
    def threshold(ytrue, ypred):
        fpr, tpr, thresholds = metrics.roc_curve(ytrue, ypred)
        y = tpr - fpr
        youden_index = np.argmax(y)
        optimal_threshold = thresholds[youden_index]
        point = [fpr[youden_index], tpr[youden_index]]
        roc_auc = metrics.auc(fpr, tpr)
        return optimal_threshold, point, fpr, tpr, roc_auc
    statistic_threshold, statistic_point, statistic_fpr, statistic_tpr, statis = threshold(ytrue, ypred)
    return statistic_threshold, statistic_point, statistic_fpr, statistic_tpr, statis


def plot_roc_curve(fpr, tpr, auc_score):
    f = plt.figure(figsize=None)
    plt.plot(fpr, tpr, label=f'AUC = {auc_score:.4f}')
    plt.plot([0, 1], [0, 1], linestyle='--', color='r', label='Random Classifier')
    plt.xlabel('False Positive Rate')  
    plt.ylabel('True Positive Rate')  
    plt.title('ROC Curve')  
    plt.legend()
    return f



def test(test_loader, path_ckpt, dict_test, dict_label, dict_new):
    with torch.no_grad():
        model = resnet50()
        model = torch.nn.DataParallel(model).to(device)
        model_ckpt = torch.load(path_ckpt)
        model.load_state_dict(model_ckpt['model_state_dict'])
        model.eval()
        model.to(device)

        loss_sum = 0
        acc_2 = 0
        acc_4 = 0
        num_class = 2
        conf_matrix = torch.zeros(num_class, num_class)

        for step, data in tqdm(enumerate(test_loader)):
            img = data['img'].to(device)
            targets = data['label'].to(device)
            idd = data['id']

            three_dimension = []
            for i in idd:
                three_dimension.append(dict_new[i])
            torch_three_dimension = torch.Tensor(three_dimension)

            outputs = model(img, torch_three_dimension).squeeze(1)
 
            loss = F.cross_entropy(outputs, torch.max(targets, 1)[1]).to(device)

            loss_sum += loss.detach().item()
            outputs = F.softmax(outputs, 1)

            for i in range(len(idd)):
                dict_test[str(idd[i])].append(outputs[i])

        loss_avg = loss_sum / len(test_loader)

        ll_ypred = []
        ll_ytrue = []

        for key in dict_test:
            ll = torch.tensor([0.0, 0.0]).to(device)
            for i in dict_test[key]:
                ll += i
            ll /= len(dict_test[key])

            l_positive = ll.cpu().numpy()[0]
            ll_ypred.append(l_positive)

            prediction = torch.max(ll.unsqueeze(0), 1)[1]
            pred_y = prediction.data.cpu().numpy()

            target = dict_label[key]
            if target == 2:
                target = torch.tensor([0, 1])
                ll_ytrue.append(0)
            else:
                target = torch.tensor([1, 0])
                ll_ytrue.append(1)

            target_pre = torch.max(target.unsqueeze(0), 1)[1].to(device)
            target_y = target_pre.data.cpu().numpy()
            ac = pred_y - target_y

            if target_y[0] == 1 and ac[0] == 0:
                acc_2 += 1
            elif target_y[0] == 0 and ac[0] == 0:
                acc_4 += 1


        ll_ytrue = np.array(ll_ytrue)
        ll_ypred = np.array(ll_ypred)

        optimal_threshold, point, fpr, tpr, roc_auc = statis_auc(ll_ytrue, ll_ypred)


        y_pred_binary = (ll_ytrue >= optimal_threshold).astype(int)

        TP = np.sum((ll_ytrue == 1) & (y_pred_binary == 1))
        TN = np.sum((ll_ytrue == 0) & (y_pred_binary == 0))
        FP = np.sum((ll_ytrue == 1) & (y_pred_binary == 0))
        FN = np.sum((ll_ytrue == 0) & (y_pred_binary == 1))
        correct_predictions = TP + TN

        SEN = TP / (TP + FN)
        SPE = TN / (TN + FP)
        mAP = (SEN + SPE) / 2
        acc = correct_predictions / (TP + TN + FP + FN)

        conf_matrix = confusion_matrix([0], [0], conf_matrix, TP)
        conf_matrix = confusion_matrix([1], [1], conf_matrix, TN)
        conf_matrix = confusion_matrix([0], [1], conf_matrix, FP)
        conf_matrix = confusion_matrix([1], [0], conf_matrix, FN)

        acc_all = (acc_2 + acc_4) / 155
        acc_2 = acc_2 / 34
        acc_4 = acc_4 / 121


        return loss_avg, conf_matrix, fpr, tpr, roc_auc, SEN, SPE, mAP, acc


def train():
    epoch = 20
    train_data = GetLoader('./T1_segment_aug', 'train')
    train_loader = DataLoader(train_data, batch_size=16, shuffle=False, pin_memory=False)
    print('train.shape: ' + str(len(train_data)))

    test_data = GetLoader('./T1_segment_aug', 'test')
    test_loader = DataLoader(test_data, batch_size=1, shuffle=False, pin_memory=False)
    print('test.shape: ' + str(len(test_data)))


    csv_file = pd.read_csv("./train.csv")
    dict_last = csv_file.loc[:, 'label'].map(lambda x: [])
    dict_last.index = csv_file['ID']
    dict_last = dict_last.to_dict()

    dict_label = csv_file.loc[:, 'label']
    dict_label.index = csv_file['ID']
    dict_label = dict_label.to_dict()

    csv_file_all = pd.read_csv("./all_data.csv")
    dict_new = csv_file_all.loc[:, 'label'].map(lambda x: [])
    dict_new.index = csv_file_all['ID']
    dict_new = dict_new.to_dict()

    three_dimension_data = pd.read_csv('./feature_extraction.csv')
    transfer = MinMaxScaler(feature_range=(0, 1))
    new_data = three_dimension_data.iloc[:, 1:]
    name = np.array(three_dimension_data.iloc[:, 0:1])
  
    new_data = transfer.fit_transform(new_data)
 
    for i in range(name.shape[0]):
        for j in range(len(new_data[i])):
            dict_new[name[i][0]].append(new_data[i][j])

    model = resnet50()
    model = torch.nn.DataParallel(model).to(device)

    resume = False
    if resume:
        path_checkpoint = './checkpoints/best_acc.pth.tar'
        checkpoint = torch.load(path_checkpoint, map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint['model_state_dict'])

    model.train()
    model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001, weight_decay=5e-4, eps=1e-8, betas=(0.9, 0.99))

    min_acc_loss = 10
    writer = SummaryWriter(comment='Linear')

    for i_epoch in range(1, epoch+1):
        loss_sum = 0
        acc_2 = 0
        acc_4 = 0
        num_class = 2
        conf_matrix_train = torch.zeros(num_class, num_class)
        for step, data in tqdm(enumerate(train_loader)):
            img = data['img'].to(device)
            targets = data['label'].to(device)
            idd = data['id']

            three_dimension = []
            for i in idd:
                three_dimension.append(dict_new[i])
            torch_three_dimension = torch.Tensor(three_dimension)

            outputs = model(img, torch_three_dimension).squeeze(1)
            loss = F.cross_entropy(outputs, torch.max(targets, 1)[1]).to(device)
            loss_sum += loss.detach().item()
            outputs = F.softmax(outputs, 1)

            for i in range(len(idd)):
                dict_last[str(idd[i])].append(outputs[i])

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        loss_avg = loss_sum / len(train_loader)

        for key in dict_last:
            ll = torch.tensor([0.0, 0.0]).to(device)
            for i in dict_last[key]:
                ll += i
            ll /= len(dict_last[key])


            prediction = torch.max(ll.unsqueeze(0), 1)[1]
            pred_y = prediction.data.cpu().numpy()

            target = dict_label[key]
            if target == 2:
                target = torch.tensor([0, 1])
            else:
                target = torch.tensor([1, 0])

            target_pre = torch.max(target.unsqueeze(0), 1)[1].to(device)
            target_y = target_pre.data.cpu().numpy()
            ac = pred_y - target_y


            if target_y[0] == 1 and ac[0] == 0:
                acc_2 += 1
            elif target_y[0] == 0 and ac[0] == 0:
                acc_4 += 1
            conf_matrix_train = confusion_matrix(prediction, target_pre, conf_matrix_train, 1)

        fig = plot_confusion_matrix(conf_matrix_train.numpy(), classes=['HGG', 'LGG'])

        writer.add_figure('confusion matrix train', fig, global_step=i_epoch)

        acc_all = (acc_2 + acc_4) / 364
        acc_2 = acc_2 / 78
        acc_4 = acc_4 / 286
        acc = (acc_2 + acc_4) / 2


        train_acc = acc * 100
        train_acc_all = acc_all * 100

        print("[Epoch " + str(i_epoch) + " | " + "train loss = " + ("%.7f" % loss_avg) + ", train mAP = " + ("%.2f" % train_acc) + "%, train acc = " + ("%.2f" % train_acc_all) + "%]")
        writer.add_scalar('train_loss', loss_avg, i_epoch)
        writer.add_scalar('train_mAP', train_acc, i_epoch)
        writer.add_scalar('train_acc', train_acc_all, i_epoch)

  
        path_ckpt = r"./checkpoints/" + str(i_epoch) + ".pth.tar"
        torch.save({"epoch": i_epoch, "model_state_dict": model.state_dict(), "optimizer_state_dict": optimizer.state_dict()}, path_ckpt)


        csv_file_2 = pd.read_csv("./test.csv")
        dict_test = csv_file_2.loc[:, 'label'].map(lambda x: [])
        dict_test.index = csv_file_2['ID']
        dict_test = dict_test.to_dict()

        dict_label_test = csv_file_2.loc[:, 'label']
        dict_label_test.index = csv_file_2['ID']
        dict_label_test = dict_label_test.to_dict()

        loss_test, confmatrix, fpr, tpr, roc_auc, SEN, SPE, acc_sum, acc_all = test(test_loader, path_ckpt, dict_test, dict_label_test, dict_new)
        test_acc = acc_sum * 100
        test_acc_all = acc_all * 100

        print("[Epoch " + str(i_epoch) + " | " + "test loss = " + ("%.7f" % loss_test) + ", test mAP = " + ("%.2f" % test_acc) + \
              "%, test acc = " + ("%.2f" % test_acc_all) + "%, test auc = " + ("%.4f" % roc_auc) + ", test SEN = " + ("%.4f" % SEN) \
              + ", test SPE = " + ("%.4f" % SPE) + "]")

        writer.add_scalar('test_loss', loss_test, i_epoch)
        writer.add_scalar('test_mAP', test_acc, i_epoch)
        writer.add_scalar('test_acc', test_acc_all, i_epoch)
        writer.add_scalar('test_auc', roc_auc, i_epoch)

        fig = plot_confusion_matrix(confmatrix.numpy(), classes=['HGG', 'LGG'])

        writer.add_figure('confusion matrix test', fig, global_step=i_epoch)

        f = plot_roc_curve(fpr, tpr, roc_auc)
        writer.add_figure('roc test', f, global_step=i_epoch)
        fpr_str = ' '.join(map(str, fpr.ravel().tolist()))
        tpr_str = ' '.join(map(str, tpr.ravel().tolist()))
        writer.add_text('fpr test', fpr_str, global_step=i_epoch)
        writer.add_text('tpr test', tpr_str, global_step=i_epoch)

        if loss_test < min_acc_loss:
            path_ckpt_best = r"./checkpoints/best_acc.pth.tar"
            torch.save({"epoch": i_epoch, "model_state_dict": model.state_dict(),
                        "optimizer_state_dict": optimizer.state_dict()}, path_ckpt_best)
            min_acc_loss = loss_test
            print("最优epoch更新为：" + str(i_epoch))

        writer.close()


if __name__ == '__main__':
    train()
