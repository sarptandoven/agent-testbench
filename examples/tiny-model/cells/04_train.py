#@title Train
import json
import time
import warnings

from sklearn.exceptions import ConvergenceWarning
from sklearn.neural_network import MLPClassifier

warnings.filterwarnings("ignore", category=ConvergenceWarning)
model = MLPClassifier(hidden_layer_sizes=(HIDDEN,), max_iter=EPOCHS, learning_rate_init=LEARNING_RATE, random_state=SEED)
began = time.time()
model.fit(X_train, y_train)
metrics = {
    "accuracy": round(float(model.score(X_test, y_test)), 4),
    "epochs": int(model.n_iter_),
    "final_loss": round(float(model.loss_), 4),
    "hidden": HIDDEN,
    "learning_rate": LEARNING_RATE,
    "seconds": round(time.time() - began, 2),
}
(OUT / "metrics.json").write_text(json.dumps(metrics, indent=2))
print(json.dumps(metrics))
