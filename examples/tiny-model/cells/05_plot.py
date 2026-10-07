#@title Where it goes wrong
import matplotlib.pyplot as plt
from sklearn.metrics import ConfusionMatrixDisplay

fig, ax = plt.subplots(figsize=(5, 5))
ConfusionMatrixDisplay.from_estimator(model, X_test, y_test, ax=ax, colorbar=False)
ax.set_title(f"accuracy {metrics['accuracy']:.3f}")
fig.tight_layout()
fig.savefig(OUT / "confusion.png", dpi=120)
plt.show()
