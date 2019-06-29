#Default training setup
./train.py --src-embeddings ./data/precalc/embeddings/en.pkl --mt-embeddings ./data/precalc/embeddings/de.pkl
--train-path ./data/dataset/train/ --dev-path ./data/dataset/dev/ --batch-size 1 --num-epochs 5 --learning-rate 0.1
--checkpoint-dir ./checkpoint --hidden-size 100