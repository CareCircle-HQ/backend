// MAIN-world shim injected at document_start on https://app.uniteus.io/*.
//
// Unite Us loads its data from separate API hosts (screenings-ingestion and
// core.uniteus.io) using a short-lived Bearer token plus x-employee-id /
// x-provider-id headers that only the page knows. This shim wraps fetch +
// XMLHttpRequest so that whenever the page calls one of those hosts we forward
// the auth headers (NOT the response body) to the isolated content script via
// window.postMessage. The isolated script then makes its own direct API calls
// (screenings, profile, insurance, etc.) without navigating the page.
//
// It never alters or blocks requests; it only observes the outgoing headers.
(function () {
  const HOSTS = ["screenings-ingestion.uniteus.io", "core.uniteus.io"];
  const matchHost = (url) =>
    !!url && HOSTS.some((h) => url.indexOf(h) !== -1);

  function readHeader(headers, name) {
    if (!headers) return null;
    const lname = name.toLowerCase();
    try {
      if (typeof Headers !== "undefined" && headers instanceof Headers) {
        return headers.get(name);
      }
      if (typeof headers.get === "function") {
        return headers.get(name);
      }
      if (Array.isArray(headers)) {
        for (const pair of headers) {
          if (pair && String(pair[0]).toLowerCase() === lname) return pair[1];
        }
        return null;
      }
      for (const k in headers) {
        if (String(k).toLowerCase() === lname) return headers[k];
      }
    } catch (_) {}
    return null;
  }

  function emit(auth, employeeId, providerId) {
    if (!auth) return;
    try {
      window.postMessage(
        {
          __uw_creds: true,
          auth: auth,
          employeeId: employeeId || "",
          providerId: providerId || "",
          ts: Date.now(),
        },
        window.location.origin
      );
    } catch (_) {}
  }

  // --- fetch wrapper -------------------------------------------------------
  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      try {
        const url =
          typeof input === "string"
            ? input
            : (input && input.url) || "";
        if (matchHost(url)) {
          // Headers can live on the Request (input) or the init object.
          const initHeaders = init && init.headers;
          const reqHeaders = input && typeof input !== "string" && input.headers;
          const get = (n) =>
            readHeader(initHeaders, n) || readHeader(reqHeaders, n);
          emit(get("authorization"), get("x-employee-id"), get("x-provider-id"));
        }
      } catch (_) {}
      return origFetch.apply(this, arguments);
    };
  }

  // --- XMLHttpRequest wrapper ---------------------------------------------
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    const origOpen = XHR.prototype.open;
    const origSetHeader = XHR.prototype.setRequestHeader;
    const origSend = XHR.prototype.send;

    XHR.prototype.open = function (method, url) {
      try {
        this.__uw_url = url;
        this.__uw_headers = {};
      } catch (_) {}
      return origOpen.apply(this, arguments);
    };

    XHR.prototype.setRequestHeader = function (name, value) {
      try {
        if (this.__uw_headers) {
          this.__uw_headers[String(name).toLowerCase()] = value;
        }
      } catch (_) {}
      return origSetHeader.apply(this, arguments);
    };

    XHR.prototype.send = function () {
      try {
        if (matchHost(String(this.__uw_url || ""))) {
          const h = this.__uw_headers || {};
          emit(h["authorization"], h["x-employee-id"], h["x-provider-id"]);
        }
      } catch (_) {}
      return origSend.apply(this, arguments);
    };
  }
})();
