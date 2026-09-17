/* Shared browser utilities. Page-specific logic stays inline in its template;
   only things more than one page needs live here. */
(function () {
  "use strict";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* Tween a counter to a new value. No-ops when the value did not change --
     the dashboard receives a status frame every 2s and re-animating an
     unchanged number is noise, not feedback. */
  function countUp(el, value) {
    const target = Number(value) || 0;
    const from = Number(el.dataset.value);
    if (Number.isFinite(from) && from === target) return;
    el.dataset.value = String(target);

    if (!Number.isFinite(from) || reduced.matches) {
      el.textContent = String(target);
      return;
    }

    el.classList.remove("is-changed");
    void el.offsetWidth; /* restart the flash animation */
    el.classList.add("is-changed");

    const start = performance.now();
    const duration = 420;
    function frame(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = String(Math.round(from + (target - from) * eased));
      if (t < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function toastRegion() {
    let region = document.getElementById("toasts");
    if (!region) {
      region = document.createElement("div");
      region.id = "toasts";
      region.className = "toasts";
      region.setAttribute("role", "status");
      region.setAttribute("aria-live", "polite");
      document.body.appendChild(region);
    }
    return region;
  }

  function toast(message, options) {
    const tone = (options && options.tone) || "ok";
    const el = document.createElement("div");
    el.className = "toast toast--" + tone;
    el.textContent = message; /* never innerHTML: messages carry server text */
    toastRegion().appendChild(el);
    setTimeout(() => {
      el.classList.add("is-leaving");
      el.addEventListener("transitionend", () => el.remove(), { once: true });
    }, 4200);
  }

  /* Replaces window.prompt for the one control that can destroy data. The
     server contract is unchanged: it still demands the exact phrase. */
  function confirmPhrase(options) {
    return new Promise((resolve) => {
      const dialog = document.createElement("dialog");
      dialog.className = "dialog";
      dialog.innerHTML =
        '<form method="dialog" class="dialog__body">' +
        '<h2 class="dialog__title"></h2>' +
        '<p class="dialog__text"></p>' +
        '<label class="field"><span class="field__label"></span>' +
        '<input class="input" type="text" autocomplete="off" spellcheck="false"></label>' +
        '<div class="dialog__actions">' +
        '<button value="cancel" class="btn btn--ghost" type="submit">Cancel</button>' +
        '<button value="confirm" class="btn btn--danger" type="submit" disabled>Enable deletions</button>' +
        "</div></form>";

      /* Static markup above; all caller-supplied text is set via textContent. */
      dialog.querySelector(".dialog__title").textContent = options.title;
      dialog.querySelector(".dialog__text").textContent = options.body;
      dialog.querySelector(".field__label").textContent =
        'Type "' + options.phrase + '" to confirm';

      const input = dialog.querySelector(".input");
      const confirm = dialog.querySelector('button[value="confirm"]');
      input.addEventListener("input", () => {
        confirm.disabled = input.value !== options.phrase;
      });

      dialog.addEventListener("close", () => {
        const ok = dialog.returnValue === "confirm" && input.value === options.phrase;
        dialog.remove();
        resolve(ok);
      });

      document.body.appendChild(dialog);
      dialog.showModal();
      input.focus();
    });
  }

  function copyButton(button, text) {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        button.classList.add("is-copied");
        const original = button.getAttribute("aria-label") || "Copy";
        button.setAttribute("aria-label", "Copied");
        setTimeout(() => {
          button.classList.remove("is-copied");
          button.setAttribute("aria-label", original);
        }, 1600);
      } catch (err) {
        toast("Could not copy to the clipboard. Select the text and copy it manually.", {
          tone: "warn",
        });
      }
    });
  }

  function subscribeStatus(handler) {
    const source = new EventSource("/events");
    source.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      handler(data);
    };
    return source;
  }

  window.igp = { countUp, toast, confirmPhrase, copyButton, subscribeStatus };
})();
