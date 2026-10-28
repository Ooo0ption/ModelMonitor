/* Only allowlisted metadata is emitted. No inference from generated prose. */
(() => {
  const clean = v => typeof v === 'string' ? v.slice(0, 180) : '';
  const fields = o => Object.fromEntries(['model_slug', 'model', 'default_model_slug']
    .filter(k => clean(o?.[k])).map(k => [k, clean(o[k])]));
  class MetadataParser {
    constructor(emit) { this.emit = emit; this.buffer = ''; this.last = null; this.seen = new Map(); }
    message(m) {
      if (!m || m.author?.role !== 'assistant' || !clean(m.id)) { this.last=null; return; }
      this.last = clean(m.id);
      this.update(this.last, fields(m.metadata), fields(m));
    }
    update(id, metadata = {}, top = {}) {
      const previous = this.seen.get(id) || {message_id:id, metadata:{}, message_fields:{}};
      const next = {message_id:id, metadata:{...previous.metadata,...metadata},
        message_fields:{...previous.message_fields,...top}};
      if (JSON.stringify(next) === JSON.stringify(previous) && this.seen.has(id)) return;
      if (this.seen.size >= 64 && !this.seen.has(id)) return;
      this.seen.set(id, next); this.emit(next);
    }
    object(o, depth = 0) {
      if (!o || typeof o !== 'object' || depth > 6) return;
      if (Array.isArray(o)) { o.slice(0,100).forEach(v => this.object(v, depth+1)); return; }
      if (o.message) this.message(o.message);
      // Delta streams can replace a whole message, metadata, or one metadata field.
      const path = o.p ?? o.path;
      const value = Object.hasOwn(o,'v') ? o.v : o.value;
      const op = o.o ?? o.op;
      if (path === '/message' && ['add','replace'].includes(op)) this.message(value);
      if (this.last && ['add','replace'].includes(op)) {
        if (path === '/message/metadata') this.update(this.last, fields(value));
        const match = /^\/message\/metadata\/(model_slug|model|default_model_slug)$/.exec(path || '');
        if (match && clean(value)) this.update(this.last, {[match[1]]:clean(value)});
      }
      // Only protocol wrappers; never recurse into content or arbitrary text.
      if (o.v && typeof o.v === 'object') this.object(o.v, depth+1);
      if (Array.isArray(o.patches)) this.object(o.patches, depth+1);
    }
    frame(frame) {
      const data = frame.split('\n').filter(l => l.startsWith('data:'))
        .map(l => l.slice(5).replace(/^ /,'')).join('\n');
      if (!data || data === '[DONE]') return;
      try { this.object(JSON.parse(data)); } catch { /* Non-JSON protocol frames. */ }
    }
    push(chunk, final = false) {
      this.buffer += chunk;
      let match;
      while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
        this.frame(this.buffer.slice(0,match.index).replace(/\r\n/g,'\n'));
        this.buffer = this.buffer.slice(match.index+match[0].length);
      }
      if (this.buffer.length > 2_000_000) throw new Error('frame_limit');
      if (final && this.buffer.trim()) { this.frame(this.buffer.replace(/\r\n/g,'\n')); this.buffer=''; }
    }
  }
  globalThis.ModelTrackerParser = MetadataParser;
})();
