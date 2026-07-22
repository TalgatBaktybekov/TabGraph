// TabGraph content script.
(() => {
  try {
    // Grab the raw HTML before anything touches the page; the knowledge
    // layer is derived and re-generatable, raw capture is not.
    const html = document.documentElement.outerHTML;
    // Readability mutates the DOM it parses, so use a clone.
    const article = new Readability(document.cloneNode(true)).parse();
    return {
      ok: true,
      title: article?.title || document.title,
      text: (article?.textContent ?? "").trim(),
      html,
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})();
