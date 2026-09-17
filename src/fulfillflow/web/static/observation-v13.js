/* Page-local read observation: no admission retries, rearm or business mutation. */
(() => {
  let current = null;
  const element = () => document.querySelector("[data-observation-id]");
  const identity = (node) => node?.dataset.observationId || node?.dataset.observationRenew;
  function message(node, text) {
    const status = node.querySelector("[data-observation-message]");
    if (status) status.textContent = text;
  }
  function observe() {
    const node = element();
    if (!node) { current = null; return; }
    const id = identity(node);
    if (!current || current.id !== id) {
      current = {id, deadline: performance.now() + 30000, busy: false};
    }
    if (node.dataset.observationPoll === "false") return;
    if (performance.now() >= current.deadline) {
      message(node, (current.failed ? "Query unavailable at last attempt. " : "") + "Observation deadline reached. The last observation is preserved; this does not mean rejection or loss. Use Check again to start a new observation.");
      return;
    }
    if (!current.busy) htmx.trigger(node, "observe");
  }
  document.addEventListener("htmx:beforeRequest", (event) => {
    const source = event.detail.requestConfig.elt;
    const id = identity(source);
    if (!id) return;
    if (source.hasAttribute("data-observation-renew")) {
      if (current?.busy) { event.preventDefault(); return; }
      current = {id, deadline: performance.now() + 30000, busy: false};
    }
    if (!current || current.id !== id || performance.now() >= current.deadline) {
      event.preventDefault();
    } else current.busy = true;
  });
  document.addEventListener("htmx:beforeSwap", (event) => {
    const id = identity(event.detail.requestConfig.elt);
    if (!id) return;
    if (event.detail.xhr.status !== 200 || identity(element()) !== id ||
        !current || current.id !== id || performance.now() >= current.deadline) {
      event.detail.shouldSwap = false;
    }
  });
  document.addEventListener("htmx:afterRequest", (event) => {
    const id = identity(event.detail.requestConfig.elt);
    if (!id || !current || current.id !== id) return;
    current.busy = false;
    current.failed = Boolean(event.detail.failed);
    const node = element();
    if (identity(node) === id && event.detail.failed && performance.now() < current.deadline) {
      message(node, "Query unavailable. The last observation and Tracking/Order result are unchanged. No webhook was resent. Use Check again or wait within the observation deadline.");
    }
  });
  let timer = setInterval(observe, 1000);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      current = null;
      clearInterval(timer);
      timer = setInterval(observe, 1000);
    }
  });
  window.addEventListener("pagehide", () => { clearInterval(timer); current = null; });
})();
