#!/bin/bash
#SBATCH -p nvidia
#SBATCH --gres=gpu:a100:1
#SBATCH --time=1-23:59:59
#SBATCH --cpus-per-task=18
# Output and error files
#SBATCH -o outlogs/job.%J.out
#SBATCH -e errlogs/job.%J.err
    
# Activating conda
eval "$(conda shell.bash hook)"
conda activate medfuse

python fusion_main.py \
--mode train \
--epochs 50 --batch_size 16 --lr 0.0005 \
--num_classes 1 \
--modalities EHR \
--pretraining EHR \
--order test_run \
--data_pairs paired \
--H_mode unimodal \
--save_dir checkpoints/ \
--task in-hospital-mortality \
--labels_set mortality

