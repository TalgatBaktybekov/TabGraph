// TabGraph content script.
(() => {
  try {
    // Readability mutates the DOM it parses, so use a clone.
    const article = new Readability(document.cloneNode(true)).parse();
    return {
      ok: true,
      title: article?.title || document.title,
      text: (article?.textContent ?? "").trim(),
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})();
