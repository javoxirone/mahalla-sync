const syncForm = document.getElementById("sync-form");
const syncButton = document.getElementById("sync-button");
const message = document.getElementById("sync-message");

syncForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  syncButton.disabled = true;
  syncButton.textContent = "Synchronizing…";
  message.hidden = false;
  message.className = "";
  message.textContent = "Fetching and checking the source. Your current data stays available.";
  try {
    const response = await fetch(syncForm.action, {
      method: "POST",
      headers: { "X-CSRFToken": syncForm.elements.csrfmiddlewaretoken.value },
      credentials: "same-origin",
    });
    const result = await response.json();
    // A logged failure is displayed by the refreshed page, alongside its history.
    if (response.status === 502) {
      window.location.reload();
      return;
    }
    if (!response.ok) throw new Error(result.error || "Synchronization could not be completed.");
    window.location.reload();
  } catch (error) {
    message.className = "error";
    message.textContent = error.message + " Refresh the page to see the latest status.";
    syncButton.disabled = false;
    syncButton.textContent = "Synchronize Now";
  }
});

document.getElementById("region").addEventListener("change", (event) => {
  document.getElementById("district").value = "";
  event.target.form.requestSubmit();
});
