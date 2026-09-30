/* @ds-bundle: {"format":4,"namespace":"Huddle","components":[{"name":"MessageGroup"},{"name":"MessageBubble"},{"name":"Receipt"},{"name":"AgentIndicator"},{"name":"SystemNote"},{"name":"Composer"},{"name":"EmptyState"},{"name":"ThreadSkeleton"},{"name":"ThreadHeader"},{"name":"SessionDetails"},{"name":"BudgetMeter"},{"name":"JoinForm"},{"name":"Avatar"},{"name":"Icon"},{"name":"Logo"},{"name":"Button"},{"name":"SessionRow"}]} */
(function () {
  var React = window.React;
  var h = React.createElement;
  var useState = React.useState;

  function cx() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) if (arguments[i]) out.push(arguments[i]);
    return out.join(' ');
  }
  function omit(obj, keys) {
    var o = {};
    for (var k in obj) if (Object.prototype.hasOwnProperty.call(obj, k) && keys.indexOf(k) < 0) o[k] = obj[k];
    return o;
  }
  function initials(name) {
    var parts = String(name || '?').trim().split(/\s+/);
    var s = parts[0].charAt(0) + (parts.length > 1 ? parts[parts.length - 1].charAt(0) : '');
    return s.toUpperCase();
  }
  function fmtTime(t) {
    if (!t) return '';
    var d = t instanceof Date ? t : new Date(t);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  function fmtTokens(n) {
    if (n >= 1000) return (Math.round(n / 100) / 10).toString().replace(/\.0$/, '') + 'k';
    return String(Math.round(n));
  }

  var ICONS = {"bell-off": "<path d=\"M10.268 21a2 2 0 0 0 3.464 0\" /> <path d=\"M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742\" /> <path d=\"m2 2 20 20\" /> <path d=\"M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05\" />", "bell": "<path d=\"M10.268 21a2 2 0 0 0 3.464 0\" /> <path d=\"M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326\" />", "check-check": "<path d=\"M18 6 7 17l-5-5\" /> <path d=\"m22 10-7.5 7.5L13 16\" />", "check": "<path d=\"M20 6 9 17l-5-5\" />", "chevron-down": "<path d=\"m6 9 6 6 6-6\" />", "circle-alert": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <line x1=\"12\" x2=\"12\" y1=\"8\" y2=\"12\" /> <line x1=\"12\" x2=\"12.01\" y1=\"16\" y2=\"16\" />", "clock": "<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"M12 6v6l4 2\" />", "copy": "<rect width=\"14\" height=\"14\" x=\"8\" y=\"8\" rx=\"2\" ry=\"2\" /> <path d=\"M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2\" />", "download": "<path d=\"M12 15V3\" /> <path d=\"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4\" /> <path d=\"m7 10 5 5 5-5\" />", "ellipsis": "<circle cx=\"12\" cy=\"12\" r=\"1\" /> <circle cx=\"19\" cy=\"12\" r=\"1\" /> <circle cx=\"5\" cy=\"12\" r=\"1\" />", "log-out": "<path d=\"m16 17 5-5-5-5\" /> <path d=\"M21 12H9\" /> <path d=\"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4\" />", "message-square": "<path d=\"M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z\" />", "paperclip": "<path d=\"m16 6-8.414 8.586a2 2 0 0 0 2.829 2.829l8.414-8.586a4 4 0 1 0-5.657-5.657l-8.379 8.551a6 6 0 1 0 8.485 8.485l8.379-8.551\" />", "pause": "<rect x=\"14\" y=\"3\" width=\"5\" height=\"18\" rx=\"1\" /> <rect x=\"5\" y=\"3\" width=\"5\" height=\"18\" rx=\"1\" />", "pin": "<path d=\"M12 17v5\" /> <path d=\"M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z\" />", "play": "<path d=\"M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z\" />", "plus": "<path d=\"M5 12h14\" /> <path d=\"M12 5v14\" />", "reply": "<path d=\"M20 18v-2a4 4 0 0 0-4-4H4\" /> <path d=\"m9 17-5-5 5-5\" />", "rotate-ccw": "<path d=\"M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8\" /> <path d=\"M3 3v5h5\" />", "search": "<path d=\"m21 21-4.34-4.34\" /> <circle cx=\"11\" cy=\"11\" r=\"8\" />", "send": "<path d=\"M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z\" /> <path d=\"m21.854 2.147-10.94 10.939\" />", "settings": "<path d=\"M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915\" /> <circle cx=\"12\" cy=\"12\" r=\"3\" />", "smile": "<path d=\"M15 10V9\" /> <path d=\"M16.472 15a6 6 0 01-8.943 0\" /> <path d=\"M9 10V9\" /> <circle cx=\"12\" cy=\"12\" r=\"10\" />", "trash-2": "<path d=\"M10 11v6\" /> <path d=\"M14 11v6\" /> <path d=\"M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6\" /> <path d=\"M3 6h18\" /> <path d=\"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2\" />", "user-plus": "<path d=\"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2\" /> <circle cx=\"9\" cy=\"7\" r=\"4\" /> <line x1=\"19\" x2=\"19\" y1=\"8\" y2=\"14\" /> <line x1=\"22\" x2=\"16\" y1=\"11\" y2=\"11\" />", "users": "<path d=\"M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2\" /> <path d=\"M16 3.128a4 4 0 0 1 0 7.744\" /> <path d=\"M22 21v-2a4 4 0 0 0-3-3.87\" /> <circle cx=\"9\" cy=\"7\" r=\"4\" />", "wifi-off": "<path d=\"M12 20h.01\" /> <path d=\"M8.5 16.429a5 5 0 0 1 7 0\" /> <path d=\"M5 12.859a10 10 0 0 1 5.17-2.69\" /> <path d=\"M19 12.859a10 10 0 0 0-2.007-1.523\" /> <path d=\"M2 8.82a15 15 0 0 1 4.177-2.643\" /> <path d=\"M22 8.82a15 15 0 0 0-11.288-3.764\" /> <path d=\"m2 2 20 20\" />", "x": "<path d=\"M18 6 6 18\" /> <path d=\"m6 6 12 12\" />"};

  /* ---------- Icon, Button ---------- */
  function Icon(props) {
    var size = props.size || 20;
    var inner = ICONS[props.name];
    if (!inner) return null;
    var a = { xmlns: 'http://www.w3.org/2000/svg', width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      strokeWidth: props.strokeWidth || (size <= 16 ? 2 : 1.5), strokeLinecap: 'round', strokeLinejoin: 'round',
      className: cx('hd-icon', props.className), dangerouslySetInnerHTML: { __html: inner } };
    if (props.label) { a.role = 'img'; a['aria-label'] = props.label; } else { a['aria-hidden'] = 'true'; a.focusable = 'false'; }
    return h('svg', a);
  }
  Icon.names = Object.keys(ICONS);

  function Button(props) {
    var variant = props.variant || 'quiet';
    var size = props.size || 'md';
    var rest = omit(props, ['variant', 'size', 'className', 'children', 'icon', 'label', 'block']);
    if (!rest.type) rest.type = 'button';
    var iconOnly = props.icon && props.children == null;
    if (iconOnly) rest['aria-label'] = props.label;
    rest.className = cx('hd-btn', 'hd-btn-' + variant, 'hd-btn-' + size, iconOnly && 'hd-btn-icon', props.block && 'hd-btn-block', props.className);
    return h('button', rest, props.icon ? h(Icon, { name: props.icon, size: size === 'sm' ? 16 : 20 }) : null, props.children);
  }

  /* ---------- Avatar ---------- */
  function Avatar(props) {
    var kind = props.kind || 'person';
    var size = props.size || 'md';
    var label = props.name + (kind === 'agent' ? ' (AI)' : '');
    var letter = kind === 'agent' || size === 'sm' ? String(props.name || 'AI').charAt(0).toUpperCase() : initials(props.name);
    return h('span', { className: cx('hd-avatar', 'hd-avatar-' + kind, 'hd-avatar-' + size, props.idle && 'hd-avatar-idle', props.className), role: 'img', 'aria-label': label },
      h('span', { 'aria-hidden': 'true' }, letter));
  }

  /* ---------- Receipt: sending, one, two, three checks ---------- */
  var CHECK = 'M20 6 9 17l-5-5';
  function Receipt(props) {
    var status = props.status;
    var agent = props.agentName || 'Huddle AI';
    var labels = { sending: 'Sending', stored: 'Sent', delivered: 'Delivered', 'agent-read': 'Read by ' + agent };
    var label = labels[status];
    if (!label) return null;
    if (status === 'sending') {
      return h('span', { className: cx('hd-receipt', props.className), role: 'img', 'aria-label': label, title: label }, h(Icon, { name: 'clock', size: 14 }));
    }
    var n = status === 'stored' ? 1 : status === 'delivered' ? 2 : 3;
    var ticks = [];
    for (var i = 0; i < n; i++) {
      ticks.push(h('path', { key: i, d: CHECK, transform: 'translate(' + (i * 10) + ' 0)', className: i === 2 ? 'hd-tick-agent' : 'hd-tick' }));
    }
    return h('span', { className: cx('hd-receipt', props.className), role: 'img', 'aria-label': label, title: label },
      h('svg', { className: 'hd-receipt-svg', width: 29, height: 16, viewBox: '0 0 44 24', 'aria-hidden': 'true', focusable: 'false' }, ticks));
  }

  /* ---------- MessageBubble ---------- */
  var ERRORS = {
    DEFAULT: { text: 'Didn’t send', retry: true },
    RATE_LIMITED: { text: 'Sending too fast', retry: true },
    MESSAGE_TOO_LONG: { text: 'Too long to send (4,000 characters max)', retry: false },
    NOT_MEMBER: { text: 'You’re not in this huddle', retry: false },
    SESSION_CLOSED: { text: 'This huddle has ended', retry: false }
  };

  function MessageBubble(props) {
    var from = props.from || 'other';
    var status = props.status;
    var failed = status === 'failed';
    var err = failed ? (ERRORS[props.errorCode] || ERRORS.DEFAULT) : null;
    var receipt = from === 'self' && status && !failed ? h(Receipt, { status: status, agentName: props.agentName }) : null;
    var bubble = h('div', { className: cx('hd-bubble', 'hd-bubble-' + from, props.tail && 'hd-tail', failed && 'hd-bubble-failed', props.className) }, props.children);
    return h('div', { className: cx('hd-bubble-wrap', 'hd-from-' + from) },
      h('div', { className: 'hd-bubble-row' }, receipt, bubble),
      failed ? h('div', { className: 'hd-failed', role: 'status' }, err.text,
        err.retry ? h(React.Fragment, null, '. ', h('button', { type: 'button', className: 'hd-link', onClick: props.onRetry }, 'Retry')) : null) : null);
  }

  /* ---------- MessageGroup ---------- */
  function MessageGroup(props) {
    var kind = props.kind || 'other';
    var msgs = props.messages || [];
    var self = kind === 'self';
    return h('div', { className: cx('hd-group', 'hd-group-' + kind, props.className) },
      self ? null : h(Avatar, { name: props.author, kind: kind === 'agent' ? 'agent' : 'person', size: 'md', className: 'hd-group-avatar' }),
      h('div', { className: 'hd-group-body' },
        h('div', { className: 'hd-group-head' },
          self ? null : h('span', { className: cx('hd-sender', kind === 'agent' && 'hd-sender-agent') }, props.author),
          props.time ? h('span', { className: 'hd-time' }, props.time) : null),
        msgs.map(function (m, i) {
          var text = typeof m === 'string' ? m : m.text;
          var o = typeof m === 'string' ? {} : m;
          return h(MessageBubble, { key: i, from: kind, tail: i === msgs.length - 1, status: o.status, errorCode: o.errorCode, onRetry: o.onRetry, agentName: props.agentName }, text);
        })));
  }

  /* ---------- AgentIndicator ---------- */
  function AgentIndicator(props) {
    var name = props.name || 'Huddle AI';
    var state = props.state || 'following';
    if (state === 'listening') state = 'following';
    var idle = state === 'paused' || state === 'resting' || state === 'away';
    var text = state === 'writing' ? ' is writing'
      : state === 'paused' ? ' is paused'
      : state === 'away' ? ' is away right now'
      : state === 'resting' ? ' is resting' + (props.backAt ? ' · back at ' + fmtTime(props.backAt) : '')
      : ' is following along';
    return h('div', { className: cx('hd-agent-ind', idle && 'hd-agent-ind-idle', props.className), role: 'status', 'aria-live': 'polite' },
      h(Avatar, { name: name, kind: 'agent', size: 'sm', idle: idle }),
      h('span', { className: 'hd-agent-ind-text' }, h('span', { className: 'hd-agent-ind-name' }, name), text),
      state === 'writing' ? h('span', { className: 'hd-dots', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')) : null);
  }

  /* ---------- SystemNote ---------- */
  function SystemNote(props) {
    return h('div', { className: cx('hd-note', props.className), role: 'note' },
      h('span', null, props.children), props.time ? h('span', { className: 'hd-note-time' }, props.time) : null);
  }

  /* ---------- Composer ---------- */
  function Composer(props) {
    var st = useState('');
    var value = st[0], setValue = st[1];
    var state = props.state || 'ready';
    var max = props.maxLength || 4000;
    if (state === 'closed') {
      return h('div', { className: cx('hd-composer-closed', props.className), role: 'note' },
        h('span', null, props.closedText || 'This huddle has ended'),
        props.onExport ? h(Button, { variant: 'quiet', size: 'sm', icon: 'download', onClick: props.onExport }, 'Export') : null);
    }
    var locked = state === 'connecting' || state === 'reconnecting' || props.disabled;
    var placeholder = state === 'connecting' ? 'Connecting…' : state === 'reconnecting' ? 'Reconnecting…' : (props.placeholder || 'Message the huddle');
    var over = value.length - max;
    var showCount = value.length >= Math.floor(max * 0.9);
    var can = value.trim().length > 0 && !locked && over <= 0;
    function send() {
      if (!can) return;
      if (props.onSend) props.onSend(value.trim());
      setValue('');
    }
    function grow(el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 160) + 'px';
    }
    return h('form', { className: cx('hd-composer', locked && 'hd-composer-disabled', props.className), onSubmit: function (e) { e.preventDefault(); send(); } },
      h('textarea', {
        rows: 1, className: 'hd-composer-input', value: value, disabled: locked,
        placeholder: placeholder, 'aria-label': placeholder,
        onChange: function (e) { setValue(e.target.value); grow(e.target); },
        onKeyDown: function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }
      }),
      showCount ? h('span', { className: cx('hd-count', over > 0 && 'hd-count-over'), 'aria-live': 'polite' }, value.length.toLocaleString() + ' / ' + max.toLocaleString()) : null,
      h(Button, { type: 'submit', variant: 'primary', size: 'sm', icon: 'send', disabled: !can }, 'Send'));
  }

  /* ---------- EmptyState, ThreadSkeleton ---------- */
  function EmptyState(props) {
    return h('div', { className: cx('hd-empty', props.className) },
      h('p', { className: 'hd-empty-title' }, 'Nobody’s said anything yet.'),
      h('p', { className: 'hd-empty-body' }, 'Start the huddle.'),
      props.children);
  }

  function ThreadSkeleton(props) {
    var rows = [['other', '46%'], ['other', '30%'], ['agent', '62%'], ['self', '34%'], ['other', '52%']];
    return h('div', { className: cx('hd-skel', props.className), role: 'status', 'aria-label': 'Loading messages' },
      rows.map(function (r, i) {
        return h('div', { key: i, className: cx('hd-skel-row', r[0] === 'self' && 'hd-skel-self') },
          r[0] === 'self' ? null : h('span', { className: 'hd-skel-av' }),
          h('span', { className: 'hd-skel-bar', style: { width: r[1] } }));
      }));
  }

  /* ---------- ThreadHeader ---------- */
  function ConnectionDot(props) {
    var labels = { connected: 'Connected', connecting: 'Connecting', reconnecting: 'Reconnecting' };
    var label = labels[props.state];
    if (!label) return null;
    return h('span', { className: 'hd-conn hd-conn-' + props.state, role: 'img', 'aria-label': label, title: label });
  }

  function ThreadHeader(props) {
    var members = props.members || [];
    var agent = props.agent || { name: 'Huddle AI', state: 'following' };
    var conn = props.connection || 'connected';
    var idle = agent.state === 'paused' || agent.state === 'resting' || agent.state === 'away';
    var shown = members.slice(0, 4);
    var extra = members.length - shown.length;
    var people = members.length + (members.length === 1 ? ' person' : ' people');
    return h('header', { className: cx('hd-thread-head', props.className) },
      h('div', { className: 'hd-thread-head-main' },
        h('div', { className: 'hd-thread-title-row' },
          h('h1', { className: 'hd-thread-title' }, props.title),
          h(ConnectionDot, { state: conn })),
        h('div', { className: 'hd-thread-sub' },
          conn === 'closed' ? h('span', null, 'Ended') :
          idle ? h(React.Fragment, null, h('span', null, people), h('span', { 'aria-hidden': 'true' }, '·'),
            h(AgentIndicator, { name: agent.name, state: agent.state, backAt: agent.backAt }))
          : h('span', null, people + ' and ' + agent.name))),
      h('button', { type: 'button', className: 'hd-facepile', onClick: props.onOpenDetails, 'aria-label': 'Open huddle details' },
        shown.map(function (m, i) { return h(Avatar, { key: i, name: m.name, size: 'sm' }); }),
        extra > 0 ? h('span', { className: 'hd-more' }, '+' + extra) : null,
        h(Avatar, { name: agent.name, kind: 'agent', size: 'sm', idle: idle })));
  }

  /* ---------- BudgetMeter, SessionDetails ---------- */
  function BudgetMeter(props) {
    var cap = props.capacity || 1;
    var rem = Math.max(0, Math.min(cap, props.remaining || 0));
    var pct = Math.round((rem / cap) * 100);
    var resting = props.resting || rem === 0;
    return h('div', { className: cx('hd-budget', props.className) },
      h('div', { className: 'hd-budget-head' },
        h('span', { className: 'hd-budget-label' }, (props.name || 'Huddle AI') + ' budget'),
        h('span', { className: 'hd-budget-num' }, fmtTokens(rem) + ' of ' + fmtTokens(cap) + ' tokens')),
      h('div', { className: 'hd-meter', role: 'meter', 'aria-label': 'AI budget remaining', 'aria-valuemin': 0, 'aria-valuemax': cap, 'aria-valuenow': rem, 'aria-valuetext': pct + '% left' },
        h('span', { style: { width: pct + '%' } })),
      h('p', { className: 'hd-budget-note' }, resting && props.refillsAt ? 'Resting until ' + fmtTime(props.refillsAt) : 'Refills continuously'));
  }

  function SessionDetails(props) {
    var members = props.members || [];
    var agent = props.agent || { name: 'Huddle AI', state: 'following' };
    var ed = useState(false), editing = ed[0], setEditing = ed[1];
    var dr = useState(agent.name), draft = dr[0], setDraft = dr[1];
    var budget = agent.budget;
    function save() {
      var v = draft.trim();
      if (v && props.onRename) props.onRename(v);
      setEditing(false);
    }
    var canPause = agent.state === 'following' || agent.state === 'writing';
    return h('aside', { className: cx('hd-details', props.className), 'aria-label': 'Huddle details' },
      h('div', { className: 'hd-details-head' },
        h('h2', { className: 'hd-details-title' }, 'Details'),
        props.onClose ? h(Button, { variant: 'ghost', size: 'sm', icon: 'x', label: 'Close details', onClick: props.onClose }) : null),
      h('section', { className: 'hd-details-sec' },
        h('h3', { className: 'hd-sec-title' }, 'AI'),
        h(AgentIndicator, { name: agent.name, state: agent.state, backAt: agent.backAt }),
        editing
          ? h('form', { className: 'hd-rename', onSubmit: function (e) { e.preventDefault(); save(); } },
              h('input', { className: 'hd-input', value: draft, 'aria-label': 'AI name', autoFocus: true, onChange: function (e) { setDraft(e.target.value); } }),
              h(Button, { type: 'submit', variant: 'primary', size: 'sm', disabled: !draft.trim() }, 'Save'),
              h(Button, { variant: 'ghost', size: 'sm', onClick: function () { setEditing(false); } }, 'Cancel'))
          : h('div', { className: 'hd-agent-actions' },
              agent.state === 'paused' ? h(Button, { variant: 'quiet', size: 'sm', icon: 'play', onClick: props.onResume }, 'Resume') : null,
              canPause ? h(Button, { variant: 'quiet', size: 'sm', icon: 'pause', onClick: props.onPause }, 'Pause') : null,
              h(Button, { variant: 'ghost', size: 'sm', onClick: function () { setDraft(agent.name); setEditing(true); } }, 'Rename'))),
      budget ? h('section', { className: 'hd-details-sec' },
        h('h3', { className: 'hd-sec-title' }, 'Budget'),
        h(BudgetMeter, { name: agent.name, remaining: budget.remaining, capacity: budget.capacity, refillsAt: budget.refillsAt, resting: agent.state === 'resting' })) : null,
      h('section', { className: 'hd-details-sec' },
        h('h3', { className: 'hd-sec-title' }, 'People (' + members.length + ')'),
        h('ul', { className: 'hd-people' }, members.map(function (m, i) {
          return h('li', { key: i, className: 'hd-person' }, h(Avatar, { name: m.name, size: 'sm' }), h('span', null, m.name), m.you ? h('span', { className: 'hd-person-you' }, 'you') : null);
        }))));
  }

  /* ---------- JoinForm ---------- */
  function JoinForm(props) {
    var st = useState(props.initialName || '');
    var name = st[0], setName = st[1];
    var can = name.trim().length > 0;
    return h('form', { className: cx('hd-join', props.className), onSubmit: function (e) { e.preventDefault(); if (can && props.onJoin) props.onJoin(name.trim()); } },
      h(Logo, { size: 32 }),
      h('h1', { className: 'hd-join-title' }, 'Join the huddle'),
      h('label', { className: 'hd-field' },
        h('span', { className: 'hd-field-label' }, 'Your name'),
        h('input', { className: 'hd-input', value: name, autoComplete: 'nickname', 'aria-describedby': 'hd-join-help', onChange: function (e) { setName(e.target.value); } })),
      h('p', { className: 'hd-field-help', id: 'hd-join-help' }, 'No account needed. This is the name others will see.'),
      h(Button, { type: 'submit', variant: 'primary', block: true, disabled: !can }, 'Join'));
  }

  /* ---------- Logo ---------- */
  function Logo(props) {
    var size = props.size || 32;
    var variant = props.variant || 'lockup';
    var tone = props.tone || 'soft';
    var mark = h('svg', { xmlns: 'http://www.w3.org/2000/svg', width: size, height: size, viewBox: '0 0 144 144', className: cx('hd-logo-mark', 'hd-logo-' + tone), 'aria-hidden': variant === 'lockup' ? 'true' : null, role: variant === 'mark' ? 'img' : null, 'aria-label': variant === 'mark' ? 'Huddle' : null },
      h('rect', { className: 'hd-logo-bg', width: 144, height: 144, rx: 32 }),
      h('circle', { className: 'hd-logo-fg', cx: 52, cy: 54, r: 26 }),
      h('circle', { className: 'hd-logo-fg', cx: 40, cy: 106, r: 8 }),
      h('circle', { className: 'hd-logo-fg', cx: 64, cy: 106, r: 8 }),
      h('circle', { className: 'hd-logo-fg', cx: 88, cy: 106, r: 8 }));
    if (variant === 'mark') return h('span', { className: cx('hd-logo', props.className) }, mark);
    return h('span', { className: cx('hd-logo', props.className), role: 'img', 'aria-label': 'Huddle', style: { gap: Math.round(size * 0.3) + 'px' } },
      mark, h('span', { className: 'hd-logo-word', 'aria-hidden': 'true', style: { fontSize: Math.round(size * 0.72) + 'px' } }, 'huddle'));
  }

  /* ---------- SessionRow (phase 3: several saved huddles) ---------- */
  function SessionRow(props) {
    var rest = omit(props, ['title', 'preview', 'time', 'unread', 'active', 'hasAgent', 'className']);
    rest.type = 'button';
    rest.className = cx('hd-session', props.active && 'hd-session-active', props.unread && 'hd-session-unread', props.className);
    if (props.active) rest['aria-current'] = 'true';
    return h('button', rest,
      h('span', { className: 'hd-session-top' },
        h('span', { className: 'hd-session-title' }, props.title),
        props.hasAgent ? h('span', { className: 'hd-session-agent', title: 'AI in this huddle', 'aria-label': 'AI in this huddle' }) : null,
        props.time ? h('span', { className: 'hd-session-time' }, props.time) : null),
      h('span', { className: 'hd-session-bottom' },
        h('span', { className: 'hd-session-preview' }, props.preview),
        props.unread ? h('span', { className: 'hd-unread', 'aria-label': props.unread + ' unread' }, props.unread > 99 ? '99+' : String(props.unread)) : null));
  }

  window.Huddle = Object.assign(window.Huddle || {}, {
    Button: Button, Avatar: Avatar, Icon: Icon, Logo: Logo, Receipt: Receipt,
    MessageBubble: MessageBubble, MessageGroup: MessageGroup, AgentIndicator: AgentIndicator, SystemNote: SystemNote,
    Composer: Composer, EmptyState: EmptyState, ThreadSkeleton: ThreadSkeleton, ThreadHeader: ThreadHeader,
    SessionDetails: SessionDetails, BudgetMeter: BudgetMeter, JoinForm: JoinForm, SessionRow: SessionRow
  });
})();
