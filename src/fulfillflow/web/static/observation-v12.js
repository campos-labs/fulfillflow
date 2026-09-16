/* Observation is page-local; it never retries admission or changes business state. */
(() => {
  let current = null;
  const element = () => document.querySelector("[data-observation-id]");
  function message(node, text) {
    const status = node.querySelector("[data-observation-message]");
    if (status) status.textContent = text;
  }
  function observe() {
    const node = element();
    if (!node) {
      current = null;
      return;
    }
    const id = node.dataset.observationId;
    if (!current || current.id !== id) {
      current = {id, deadline: performance.now() + 30000, busy: false};
    }
    if (performance.now() >= current.deadline) {
      message(node, "Observation deadline reached. Completion has not been observed; this does not mean rejection or loss. Use Check again to start a new observation.");
      return;
    }
    if (!current.busy) htmx.trigger(node, "observe");
  }
  document.addEventListener("htmx:beforeRequest", (event) => {
    if (event.detail.requestConfig.elt.matches("[data-observation-id]")) {
      if (!current || performance.now() >= current.deadline) event.preventDefault();
      else current.busy = true;
    }
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    if (event.detail.requestConfig.elt.matches("[data-observation-id]") &&
        (event.detail.xhr.status !== 200 || (current && performance.now() >= current.deadline))) {
      event.detail.shouldSwap = false;
    }
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    if (!event.detail.requestConfig.elt.matches("[data-observation-id]")) return;
    if (current) current.busy = false;
    const node = element();
    if (node && event.detail.failed && current && performance.now() < current.deadline) {
      message(node, "Result query failed. Acceptance is unchanged; retrying within the observation deadline.");
    }
  });
  let timer = setInterval(observe, 1000);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      current = null;
      timer = setInterval(observe, 1000);
    }
  });
  window.addEventListener("pagehide", () => clearInterval(timer));
})();
