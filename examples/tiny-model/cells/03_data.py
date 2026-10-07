#@title Data
from pathlib import Path
from sklearn.datasets import load_digits
from sklearn.model_selection import train_test_split

OUT = Path("outputs")
OUT.mkdir(exist_ok=True)
X, y = load_digits(return_X_y=True)
X = X / 16.0
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=SEED, stratify=y)
print(f"{len(X_train)} training and {len(X_test)} test images of 8x8 digits")
