let count = 0;
document.getElementById("increment").addEventListener("click", () => {
  count += 1;
  document.getElementById("count").textContent = `Count: ${count}`;
});
