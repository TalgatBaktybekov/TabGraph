// Popup: a single toggle bound to the captureEnabled flag in storage.
const toggle = document.getElementById("capture-toggle");

chrome.storage.local.get({ captureEnabled: false }).then(({ captureEnabled }) => {
  toggle.checked = captureEnabled;
});

toggle.addEventListener("change", () => {
  chrome.storage.local.set({ captureEnabled: toggle.checked });
});
