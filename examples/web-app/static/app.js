const form = document.getElementById("split");
const result = document.getElementById("result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/split", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.fromEntries(new FormData(form))),
  });
  const answer = await response.json();
  result.className = answer.error ? "error" : "";
  result.textContent = answer.error || `Each person pays $${answer.each} (of $${answer.total} with the tip).`;
});
