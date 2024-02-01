import argparse
import os
import shutil
import sys
sys.path.append('/home/ligl/project/DVAE/generation1')
sys.path.append('/home/guanlueli/project/DVAE/generation1')

import numpy as np
import torch
import torch.utils.tensorboard
from sklearn.metrics import roc_auc_score
from torch.nn.utils import clip_grad_norm_
from torch_geometric.loader import DataLoader
from torch_geometric.transforms import Compose
from tqdm.auto import tqdm
import utils.misc as misc
import utils.train as utils_train
import utils.transforms as trans
from datasets import get_dataset
from datasets.data_load import FOLLOW_BATCH
from models.amc_diff import hier_diff

base_dir_by = '/home/ligl/project/DVAE/generation1'
base_dir_hk = '/home/guanlueli/project/DVAE/generation1'
log_base_dir_by =  '/raid/ligl/data1/data_ge1'
log_base_dir_hk =  '/data/guanlueli/1Data/1Data/data_ge1'

def get_auroc(y_true, y_pred, feat_mode):
    y_true = np.array(y_true)
    y_pred = np.array(y_pred)
    avg_auroc = 0.
    possible_classes = set(y_true)
    for c in possible_classes:
        auroc = roc_auc_score(y_true == c, y_pred[:, c])
        avg_auroc += auroc * np.sum(y_true == c)
        mapping = {
            'basic': trans.MAP_INDEX_TO_ATOM_TYPE_ONLY,
            'add_aromatic': trans.MAP_INDEX_TO_ATOM_TYPE_AROMATIC,
            'full': trans.MAP_INDEX_TO_ATOM_TYPE_FULL
        }
        print(f'atom: {mapping[feat_mode][c]} \t auc roc: {auroc:.4f}')
    return avg_auroc / len(y_true)


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default='/configs/train.yml')
    parser.add_argument('--device_num', type=str, default='cuda:6')
    parser.add_argument('--logdir', type=str, default=f'/logs')
    parser.add_argument('--tag', type=str, default='s_1_lcenter_motif_time1_vtrue100_pos_001')
    parser.add_argument('--train_report', type=int, default=200)
    parser.add_argument('--base_dir', type=str, default=base_dir_hk)
    parser.add_argument('--log_base_dir', type=str, default=log_base_dir_hk)
    args = parser.parse_args()
    
    args.config = args.base_dir + args.config
    args.logdir = args.log_base_dir + args.logdir
    args.device = torch.device(args.device_num)

    # Load configs
    config = misc.load_config(args.config)
    config_name = os.path.basename(args.config)[:os.path.basename(args.config).rfind('.')]
    misc.seed_all(config.train.seed)
    args.tag =  '_' + args.tag + '_''xx' + str(config.model.x_to_x) + '_' +  'phloss' + str(config.model.ph_loss) + '_' + str(config.model.ph_loss_weight) + '_' + str(config.model.ligand_ph_feature)

    # Logging
    log_dir = misc.get_new_log_dir(args.logdir, prefix=config_name, tag=args.tag)
    print('log dir', log_dir)
    ckpt_dir = os.path.join(log_dir, 'checkpoints')
    os.makedirs(ckpt_dir, exist_ok=True)
    vis_dir = os.path.join(log_dir, 'vis')
    os.makedirs(vis_dir, exist_ok=True)
    logger = misc.get_logger('train', log_dir)
    writer = torch.utils.tensorboard.SummaryWriter(log_dir)
    logger.info(args)
    logger.info(config)
    shutil.copyfile(args.config, os.path.join(log_dir, os.path.basename(args.config)))
    shutil.copytree(args.base_dir + '/models', os.path.join(log_dir, 'models'))

    # Transforms
    protein_featurizer = trans.FeaturizeProteinAtom()
    ligand_featurizer = trans.FeaturizeLigandAtom(config.data.transform.ligand_atom_mode)
    transform_list = [
        protein_featurizer,
        ligand_featurizer,
        trans.FeaturizeLigandBond(),
    ]
    if config.data.transform.random_rot:
        transform_list.append(trans.RandomRotation())
    transform = Compose(transform_list)

    # Datasets and loaders
    logger.info('Loading dataset...')
    dataset, subsets = get_dataset(
        config=config.data,
        transform=transform,
        version = 'final_ph1_pocket_tree_torch_float_v1'
    )
    train_set, val_set = subsets['train'], subsets['test']
    logger.info(f'Training: {len(train_set)} Validation: {len(val_set)}')


    # follow_batch = ['protein_element', 'ligand_element']
    collate_exclude_keys = ['ligand_nbh_list']
    train_iterator = utils_train.inf_iterator(DataLoader(
        train_set,
        batch_size=config.train.batch_size,
        shuffle=True,
        num_workers=config.train.num_workers,
        follow_batch=FOLLOW_BATCH,
        exclude_keys=collate_exclude_keys
    ))
    val_loader = DataLoader(val_set, config.train.batch_size, shuffle=False,
                            follow_batch=FOLLOW_BATCH, exclude_keys=collate_exclude_keys)

    train_loader = DataLoader(train_set, 1, shuffle=False,
                            follow_batch=FOLLOW_BATCH, exclude_keys=collate_exclude_keys)

    val_loader = DataLoader(val_set, 1, shuffle=False,
                            follow_batch=FOLLOW_BATCH, exclude_keys=collate_exclude_keys)

    # Model
    logger.info('Building model...')
    if config.model.checkpoint is not None:
        ckpt = torch.load(config.model.checkpoint, map_location=args.device)
        model = hier_diff(
            ckpt['config'].model,
            protein_atom_feature_dim=protein_featurizer.feature_dim,
            ligand_atom_feature_dim=ligand_featurizer.feature_dim
        ).to(args.device)
        model.load_state_dict(ckpt['model'])
        logger.info(f'Successfully load the model! {config.model.checkpoint}')
    else:
        model = hier_diff(
            config.model,
            protein_atom_feature_dim=protein_featurizer.feature_dim,
            ligand_atom_feature_dim=ligand_featurizer.feature_dim
        ).to(args.device)

    print(f'protein feature dim: {protein_featurizer.feature_dim} ligand feature dim: {ligand_featurizer.feature_dim}')
    logger.info(f'# trainable parameters: {misc.count_parameters(model) / 1e6:.4f} M')

    # Optimizer and scheduler
    optimizer = utils_train.get_optimizer(config.train.optimizer, model)
    scheduler = utils_train.get_scheduler(config.train.scheduler, optimizer)

    def train(it):
        model.train()
        optimizer.zero_grad()
        for _ in range(config.train.n_acc_batch):
            batch = next(train_iterator).to(args.device)
            protein_noise = torch.randn_like(batch.protein_pos) * config.train.pos_noise_std
            gt_protein_pos = batch.protein_pos + protein_noise

            if np.random.random() < 0.4:
                is_in_pocket = torch.zeros([batch.protein_element_batch.size(0)], device= batch.protein_element_batch.device)
            else:
                is_in_pocket = batch.is_in_pocket

            results = model.get_diffusion_loss(
                protein_pos=gt_protein_pos,
                protein_v=batch.protein_atom_feature.float(),
                protein_ph =batch.protein_ph,
                batch_protein=batch.protein_element_batch,
                batch_is_in_pocket = is_in_pocket,

                ligand_pos=batch.ligand_pos,
                ligand_v=batch.ligand_atom_feature_full,
                ligand_ph=batch.ligand_ph,
                batch_ligand=batch.ligand_element_batch,

                motif_pos = batch.motif_pos.float(),
                motif_wid = batch.node_wid,
                # motif_feature = batch.node_feature,
                batch_motif = batch.motif_pos_batch,

            )
            loss, loss_pos, loss_v, loss_pos_motif, loss_v_motif, loss_ph  = results['loss'], results['loss_pos'], results['loss_v'], results['loss_pos_motif'], results['loss_v_motif'], results['loss_ph']
            loss = loss / config.train.n_acc_batch
            loss.backward()
        orig_grad_norm = clip_grad_norm_(model.parameters(), config.train.max_grad_norm)
        optimizer.step()

        if it % args.train_report == 0:
            logger.info(
                '[Train] Iter %d | Loss %.6f (pos %.6f | v %.6f | pos_m %.6f | v_m %.6f | ph %.6f)  | Lr: %.6f | Grad Norm: %.6f' % (
                    it, loss, loss_pos, loss_v, loss_pos_motif, loss_v_motif, loss_ph, optimizer.param_groups[0]['lr'], orig_grad_norm
                )
            )
            for k, v in results.items():
                if torch.is_tensor(v) and v.squeeze().ndim == 0:
                    writer.add_scalar(f'train/{k}', v, it)
            writer.add_scalar('train/lr', optimizer.param_groups[0]['lr'], it)
            writer.add_scalar('train/grad', orig_grad_norm, it)
            writer.flush()


    def validate(it):
        # fix time steps
        sum_loss, sum_loss_pos, sum_loss_pos_motif, sum_loss_v, sum_loss_v_motif, sum_loss_ph, sum_n = 0, 0, 0, 0, 0, 0, 0
        sum_loss_bond, sum_loss_non_bond = 0, 0
        all_pred_v, all_true_v = [], []

        with torch.no_grad():
            model.eval()
            for batch in tqdm(val_loader, desc='Validate'):
                batch = batch.to(args.device)
                batch_size = batch.num_graphs
                for t in np.linspace(0, model.num_timesteps - 1, 10).astype(int):
                    time_step = torch.tensor([t] * batch_size).to(args.device)
                    results = model.get_diffusion_loss(
                        protein_pos=batch.protein_pos,
                        protein_v=batch.protein_atom_feature.float(),
                        protein_ph=batch.protein_ph,
                        batch_protein=batch.protein_element_batch,
                        batch_is_in_pocket=batch.is_in_pocket,

                        ligand_pos=batch.ligand_pos,
                        ligand_v=batch.ligand_atom_feature_full,
                        ligand_ph=batch.ligand_ph,
                        batch_ligand=batch.ligand_element_batch,

                        motif_pos=batch.motif_pos.float(),
                        motif_wid=batch.node_wid,
                        batch_motif=batch.motif_pos_batch,

                        time_step=time_step
                    )
                    loss, loss_pos, loss_pos_motif, loss_v , loss_v_motif,  loss_ph = results['loss'], results['loss_pos'], results['loss_pos_motif'], results['loss_v'], results['loss_v_motif'], results['loss_ph']

                    sum_loss += float(loss) * batch_size
                    sum_loss_pos += float(loss_pos) * batch_size
                    sum_loss_pos_motif += float(loss_pos_motif) * batch_size
                    sum_loss_v += float(loss_v) * batch_size
                    sum_loss_v_motif += float(loss_v_motif) * batch_size
                    sum_loss_ph += float(loss_ph) * batch_size

                    sum_n += batch_size
                    all_pred_v.append(results['ligand_v_recon'].detach().cpu().numpy())
                    all_true_v.append(batch.ligand_atom_feature_full.detach().cpu().numpy())

        avg_loss = sum_loss / sum_n
        avg_loss_pos = sum_loss_pos / sum_n
        avg_loss_pos_motif = sum_loss_pos_motif / sum_n
        avg_loss_v = sum_loss_v / sum_n
        avg_loss_v_motif = sum_loss_v_motif / sum_n
        avg_loss_ph = sum_loss_ph / sum_n
        atom_auroc = get_auroc(np.concatenate(all_true_v), np.concatenate(all_pred_v, axis=0), feat_mode=config.data.transform.ligand_atom_mode)

        if config.train.scheduler.type == 'plateau':
            scheduler.step(avg_loss)
        elif config.train.scheduler.type == 'warmup_plateau':
            scheduler.step_ReduceLROnPlateau(avg_loss)
        else:
            scheduler.step()

        logger.info(
            '[Validate] Iter %05d | Loss %.6f | Loss pos %.6f |  Loss pos motif %.6f | Loss v %.6f e-3 | Loss v motif %.6f e-3 | Loss ph %.6f | Avg atom auroc %.6f' % (
                it, avg_loss, avg_loss_pos, avg_loss_pos_motif, avg_loss_v * 1000, avg_loss_v_motif * 1000, avg_loss_ph,  atom_auroc
            )
        )
        writer.add_scalar('val/loss', avg_loss, it)
        writer.add_scalar('val/loss_pos', avg_loss_pos, it)
        writer.add_scalar('val/loss_pos_motif', avg_loss_pos_motif, it)
        writer.add_scalar('val/loss_v', avg_loss_v, it)
        writer.add_scalar('val/loss_v_motif', avg_loss_v_motif, it)
        writer.add_scalar('val/loss_ph', avg_loss_ph, it)
        writer.flush()
        return avg_loss


    config_path = os.path.join(ckpt_dir, 'config.pt')
    torch.save(config, config_path)


    best_loss, best_iter = None, None
    for it in range(1, config.train.max_iters + 1):
        train(it)
        if it % config.train.val_freq == 0 or it == config.train.max_iters:
            val_loss = validate(it)
            if best_loss is None or val_loss < best_loss:
                logger.info(f'[Validate] Best val loss achieved: {val_loss:.6f}')
                best_loss, best_iter = val_loss, it
                ckpt_path = os.path.join(ckpt_dir, '%d.pt' % it)
                torch.save(model.state_dict(), ckpt_path)
            else:
                logger.info(f'[Validate] Val loss is not improved. '
                            f'Best val loss: {best_loss:.6f} at iter {best_iter}')
