(function (root) {
  "use strict";
  function start(options) {
    const win = options.window || root;
    const doc = options.document || win.document;
    const now = options.now || Date.now;
    let pending = false,
      lastRecovery = -Infinity,
      lastRequest = -Infinity;
    let lastInteraction = now();
    function request() {
      if (pending || options.isLoading?.()) return;
      if (options.isInteracting()) {
        if (now() - lastInteraction < 15000) return;
        options.resetInteraction(); // Recover an abandoned pointer capture.
      }
      lastRequest = now();
      pending = true;
      try {
        Promise.resolve(options.refresh())
          .catch((error) => options.onError?.(error))
          .finally(() => {
            pending = false;
          });
      } catch (error) {
        pending = false;
        options.onError?.(error);
      }
    }
    function recover() {
      options.resetInteraction();
      if (now() - lastRecovery < 1000) return;
      lastRecovery = now();
      request();
    }
    function activity() {
      if (options.isInteracting()) lastInteraction = now();
    }
    function release() {
      options.resetInteraction();
      if (now() - lastRequest >= 15000) request();
    }
    // Embedded tabs can report hidden while visible in a side pane.
    // Browser throttling is acceptable; intentionally skipping forever is not.
    const interval = win.setInterval(request, 60000);
    doc.addEventListener("visibilitychange", recover);
    for (const event of ["focus", "pageshow", "online"]) win.addEventListener(event, recover);
    for (const event of ["pointerdown", "pointermove"]) win.addEventListener(event, activity);
    win.addEventListener("pointerup", release);
    win.addEventListener("blur", release);
    request();
    return function stop() {
      win.clearInterval(interval);
      doc.removeEventListener("visibilitychange", recover);
      for (const event of ["focus", "pageshow", "online"]) win.removeEventListener(event, recover);
      for (const event of ["pointerdown", "pointermove"]) win.removeEventListener(event, activity);
      win.removeEventListener("pointerup", release);
      win.removeEventListener("blur", release);
    };
  }
  root.HalHistoryRefresh = { start };
  if (typeof module !== "undefined" && module.exports) module.exports = { start };
})(globalThis);
