/**
 * CLU URLs  –  clu-url.js
 *
 * CLU can be served under a path prefix: a reverse proxy sends
 * X-Forwarded-Prefix: /clu and Flask (ProxyFix x_prefix=1) then builds every
 * url_for() with it. Page scripts do not go through url_for: they write
 * fetch('/api/browse'), img.src = '/api/thumbnail?...' and
 * location.href = '/reader?...', which leave the prefix and land on whatever
 * else the proxy serves at the root. This file is what makes those work.
 *
 * It must be the FIRST script on every page (base.html and login.html), so
 * nothing runs before the wrappers are in place. The prefix comes from
 * <meta name="clu-root" content="{{ request.script_root }}">; with no prefix
 * (the usual install) every function here returns its argument unchanged and
 * nothing below the helpers is installed.
 *
 *   CLU.root                  '' or '/clu'
 *   CLU.url('/api/x')         '/clu/api/x'   (idempotent; other URLs untouched)
 *   CLU.staticUrl('images/loading.svg')      an unversioned file under static/
 *
 * Covered without touching a call site: fetch, XMLHttpRequest, EventSource,
 * navigator.sendBeacon, window.open, and the src/href/action/poster of any
 * element added to or changed in the document (innerHTML included).
 *
 * NOT covered, because the browser does not let a page intercept them:
 * location.href = ..., location.assign() and location.replace(). Write those
 * as location.href = CLU.url('/reader?...'). The element net also works one
 * step late (the browser has already started the unprefixed request), so a hot
 * path, such as a grid of covers, should call CLU.url itself.
 *
 * A URL already built by the server (url_for, thumbnail_url in JSON) carries
 * the prefix; CLU.url leaves it alone.
 */
(function (window, document) {
  'use strict';

  var CLU = window.CLU = window.CLU || {};

  var meta = document.querySelector('meta[name="clu-root"]');
  var root = ((meta && meta.getAttribute('content')) || '').replace(/\/+$/, '');
  if (root && root.charAt(0) !== '/') root = '/' + root;
  CLU.root = root;

  /** True for a path on this origin that does not carry the prefix yet. */
  function needsRoot(value) {
    if (!root || typeof value !== 'string') return false;
    if (value.charAt(0) !== '/' || value.charAt(1) === '/') return false;   // relative, or //host
    if (value === root) return false;
    var next = value.charAt(root.length);
    return !(value.lastIndexOf(root, 0) === 0 && (next === '/' || next === '?' || next === '#'));
  }

  CLU.url = function (value) {
    return needsRoot(value) ? root + value : value;
  };

  CLU.staticUrl = function (file) {
    return CLU.url('/static/' + String(file).replace(/^\/+/, ''));
  };

  if (!root) return;

  // ── Requests made from script ───────────────────────────────────────────

  if (window.fetch) {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      if (typeof input === 'string') {
        input = CLU.url(input);
      } else if (window.URL && input instanceof window.URL) {
        if (input.origin === window.location.origin) {
          input = CLU.url(input.pathname) + input.search + input.hash;
        }
      }
      // A Request object was built from an absolute URL by the caller; its
      // url cannot be changed, and nothing in CLU constructs one.
      return nativeFetch.call(this, input, init);
    };
  }

  if (window.XMLHttpRequest) {
    var nativeOpen = window.XMLHttpRequest.prototype.open;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      var args = Array.prototype.slice.call(arguments);
      args[1] = CLU.url(url);
      return nativeOpen.apply(this, args);
    };
  }

  if (window.EventSource) {
    var NativeEventSource = window.EventSource;
    var PrefixedEventSource = function (url, config) {
      return new NativeEventSource(CLU.url(url), config);
    };
    PrefixedEventSource.prototype = NativeEventSource.prototype;
    PrefixedEventSource.CONNECTING = NativeEventSource.CONNECTING;
    PrefixedEventSource.OPEN = NativeEventSource.OPEN;
    PrefixedEventSource.CLOSED = NativeEventSource.CLOSED;
    window.EventSource = PrefixedEventSource;
  }

  if (window.navigator && window.navigator.sendBeacon) {
    var nativeBeacon = window.navigator.sendBeacon;
    window.navigator.sendBeacon = function (url, data) {
      return nativeBeacon.call(window.navigator, CLU.url(url), data);
    };
  }

  var nativeWindowOpen = window.open;
  window.open = function (url) {
    var args = Array.prototype.slice.call(arguments);
    if (typeof url === 'string') args[0] = CLU.url(url);
    return nativeWindowOpen.apply(window, args);
  };

  // ── URLs written into the document ──────────────────────────────────────
  // Page scripts build markup with unprefixed URLs in about 400 places. One
  // observer rewrites them as they arrive.

  var URL_ATTRS = ['src', 'href', 'action', 'poster'];
  var SELECTOR = URL_ATTRS.map(function (a) { return '[' + a + '^="/"]'; }).join(',');

  function fixElement(el) {
    for (var i = 0; i < URL_ATTRS.length; i++) {
      var value = el.getAttribute(URL_ATTRS[i]);
      if (value !== null && needsRoot(value)) el.setAttribute(URL_ATTRS[i], root + value);
    }
  }

  function fixTree(node) {
    if (node.nodeType !== 1) return;
    if (node.matches && node.matches(SELECTOR)) fixElement(node);
    if (node.querySelectorAll) {
      var found = node.querySelectorAll(SELECTOR);
      for (var i = 0; i < found.length; i++) fixElement(found[i]);
    }
  }

  if (window.MutationObserver) {
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var record = records[i];
        if (record.type === 'attributes') {
          fixElement(record.target);
        } else {
          for (var j = 0; j < record.addedNodes.length; j++) fixTree(record.addedNodes[j]);
        }
      }
    }).observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, attributeFilter: URL_ATTRS
    });
  }
})(window, document);
