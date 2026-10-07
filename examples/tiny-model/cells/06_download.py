#@title Download the metrics
from google.colab import files

files.download(str(OUT / "metrics.json"))
