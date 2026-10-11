// Minimal, safe Markdown for response cards: escape first, then add a small set
// of formatting. Only http(s) links survive. Answers are model output built from
// web content, so nothing here may produce raw HTML from the input.
'use strict';

(function () {
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        (_m, text, url) => `<a href="${url}" target="_blank" rel="noreferrer">${text}</a>`);
  }

  function render(md) {
    const lines = escapeHtml(String(md || '')).split('\n');
    const out = [];
    let list = null;
    const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
    for (const line of lines) {
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (ul || ol) {
        const tag = ul ? 'ul' : 'ol';
        if (list !== tag) { close(); out.push(`<${tag}>`); list = tag; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`);
      } else if (h) {
        close();
        out.push(`<h4>${inline(h[2])}</h4>`);
      } else if (line.trim() === '') {
        close();
      } else {
        close();
        out.push(`<p>${inline(line)}</p>`);
      }
    }
    close();
    return out.join('');
  }

  window.renderMarkdown = render;
})();
