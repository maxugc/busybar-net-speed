// Net Speed for BUSY Bar: tap Start to test your internet speed, then flip
// through the history with the wheel.
//
// The bar can't run the test itself: an app on the bar moves data at under
// 1 Mbps. A small helper program on a computer on the same network does the
// measuring (helper/netspeed_helper.py in this app's repository) and answers
// /run, /status and /stats. The helper writes its address into this app's
// storage as helper.json, so there is nothing to type.
//
// On a small computer and a very fast line the helper can't be reached while
// it downloads. It says so ("quiet"), and the app then shows the download by
// the clock, using how long the last one took, and asks again afterwards.
//
// Start: run a test, start the demo from the setup screen, or try again after
// a problem. Wheel: last test, averages for 24 hours / 7 days / 30 days,
// fastest, slowest. Back: exit. MIT licensed, see LICENSE.

const APP = "io.github.maxugc.netspeed";
const BAR = "http://localhost/api/";
const REPO = "github.com/maxugc/busybar-net-speed";
const HELPER_PORT = 8791;
const OFF = -200;                 // x for hidden elements
const GIVE_UP_MS = 90000;         // a test takes about 20 s; past this the helper is in trouble

const WHITE = "#FFFFFFFF";
const GREY = "#9AA4AEFF";
const DIM = "#30353BFF";
const GREEN = "#2ECC71FF";        // download
const BLUE = "#5DADE2FF";         // upload
const GOLD = "#F1C40FFF";
const RED = "#E53A2EFF";
const BACK_WHITE = "#FFFFFFFF";
const BACK_GREY = "#888888FF";

const PAGES = [
  { key: "last", title: "Last test" },
  { key: "h24", title: "24 hours" },
  { key: "d7", title: "7 days" },
  { key: "d30", title: "30 days" },
  { key: "best", title: "Fastest" },
  { key: "worst", title: "Slowest" },
];

let settings = { helper: "", demo: false };
let helper = null;      // the helper's address, e.g. "http://192.168.1.50:8791"
let demo = false;
let mode = "loading";   // loading | setup | offline | idle | testing | failed
let stats = null;       // answer from /stats
let statsAt = 0;        // when it arrived, to keep "5m ago" moving
let test = null;        // latest answer from /status while a test runs
let testAt = 0;         // when that answer arrived
let startedAt = 0;      // when this test began, by the bar's clock
let expect = { ping: 1.5, download: 8, quiet: false };   // from the helper: last time's timings
let problem = "";
let page = 0;

// ---------------------------------------------------------------- the bar: requests and drawing
// Requests to the bar go out one at a time: it has restarted when many
// overlapped. The bar keeps every drawn element until the same id is drawn
// again, so only changed elements are sent, an element is hidden by moving it
// off screen, z_index goes with every update, and an id keeps its type.

let barChain = Promise.resolve();
let shown = {};
let lostScreen = false;
let stopped = false;
const timers = [];

function request(url, opts) {
  const p = barChain.then(function () { return fetch(url, opts); });
  barChain = p.then(function () {}, function () {});
  return p;
}

function json(res) {
  if (!res || res.status !== 200) return null;
  return res.text().then(function (t) {
    try { return JSON.parse(t); } catch (e) { return null; }
  });
}

function barJson(path) {
  return request(BAR + path).then(json, function () { return null; });
}

function sendDraw(elements) {
  const body = JSON.stringify({ application_name: APP, elements: elements });
  request(BAR + "display/draw", { method: "POST", body: body }).then(
    function (res) {
      if (res && res.status === 200) {
        // A notification covered the app and has gone: its elements went too.
        if (lostScreen) {
          lostScreen = false;
          shown = {};
          render();
        }
      } else {
        lostScreen = true;
        shown = {};
      }
    },
    function () { shown = {}; }
  );
}

function draw(elements) {
  const out = [];
  for (let i = 0; i < elements.length; i++) {
    const text = JSON.stringify(elements[i]);
    if (shown[elements[i].id] !== text) {
      shown[elements[i].id] = text;
      out.push(elements[i]);
    }
  }
  if (out.length && !stopped) sendDraw(out);
}

function label(id, value, x, y, font, color, extra) {
  const el = { id: id, type: "text", text: value && value.length ? value : " ", x: x, y: y, align: "top_left", font: font, color: color, z_index: 2 };
  if (extra) for (const k in extra) el[k] = extra[k];
  return el;
}

function picture(id, path, x, y) {
  return { id: id, type: "image", path: path, x: x, y: y, z_index: 2 };
}

function box(id, x, y, w, color, z) {
  if (w < 1) return { id: id, type: "rectangle", x: OFF, y: 0, width: 1, height: 1, fill: "none", border_width: 0, z_index: 0 };
  return { id: id, type: "rectangle", x: x, y: y, width: w, height: 1, fill: "solid", fill_colors: [color], border_width: 0, z_index: z };
}

// parts: { l1, l1Color, r1, big, bigFont, bigColor, unit, down, up, bar, barColor, back }
function screen(parts) {
  const two = parts.down !== undefined;
  const els = [
    label("l1", parts.l1, 1, -1, "small", parts.l1Color || WHITE),
    label("r1", parts.r1 || " ", parts.r1 ? 71 : OFF, -1, "small", GREY, { align: "top_right" }),
    label("big", parts.big || " ", parts.big ? 1 : OFF, parts.bigFont === "small" ? 7 : 6, parts.bigFont || "bold", parts.bigColor || WHITE),
    label("unit", parts.unit || " ", parts.unit ? 71 : OFF, 7, "small", GREY, { align: "top_right" }),
    picture("d_icon", "images/down.png", two ? 0 : OFF, 8),
    label("d_num", two ? speed(parts.down) : " ", two ? 6 : OFF, 6, "bold", WHITE),
    picture("u_icon", "images/up.png", two ? 37 : OFF, 8),
    label("u_num", two ? speed(parts.up) : " ", two ? 43 : OFF, 6, "bold", WHITE),
    box("bar_bg", 0, 15, parts.bar !== undefined ? 72 : 0, DIM, 1),
    box("bar_fill", 0, 15, parts.bar !== undefined ? Math.round(Math.max(0, Math.min(1, parts.bar)) * 72) : 0, parts.barColor, 2),
  ];
  const back = parts.back || [parts.l1, parts.big || ""];
  for (let i = 0; i < 5; i++) {
    els.push(label("b" + i, back[i] || " ", 2, 2 + i * 15, i === 0 ? "bold" : "small", i === 0 ? BACK_WHITE : BACK_GREY, { display: "back" }));
  }
  draw(els);
}

// ---------------------------------------------------------------- words and numbers

function speed(v) {
  if (v === undefined || v === null) return "--";
  return v >= 100 ? String(Math.round(v)) : v.toFixed(1);
}

function ago(seconds) {
  const s = seconds + (Date.now() - statsAt) / 1000;
  if (s < 90) return "Just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 2 * 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function tests(n) {
  return n + (n === 1 ? " test" : " tests");
}

function known(v) {
  return v !== null && v !== undefined;
}

// ---------------------------------------------------------------- screens

function render() {
  if (stopped) return;
  if (mode === "loading") return screen({ l1: "Loading...", l1Color: GREY });
  if (mode === "setup") {
    return screen({ l1: "Run the helper", big: "Start: try demo", bigFont: "small", bigColor: GREY,
      back: ["Net Speed", "Needs its helper program on a", "computer on your network. Get it at", REPO, "Start: try it with demo data"] });
  }
  if (mode === "offline") {
    return screen({ l1: "Helper is offline", l1Color: RED, big: "Start: try again", bigFont: "small", bigColor: GREY,
      back: ["Helper is offline", "The helper program isn't answering.", "Check it is still running on your", "computer, on the same network.", "Start: try again"] });
  }
  if (mode === "failed") {
    return screen({ l1: "Test failed", l1Color: RED, big: "Start: try again", bigFont: "small", bigColor: GREY,
      back: ["Test failed", problem.slice(0, 34), problem.slice(34, 68), problem.slice(68, 102), "Start: try again"] });
  }
  if (mode === "testing") return renderTest();
  renderPage();
}

function renderTest() {
  const t = test || {};
  const elapsed = (Date.now() - startedAt) / 1000;
  const fresh = Date.now() - testAt < 1500;
  const back = [
    demo ? "Testing... (demo)" : "Testing...",
    "Ping: " + (known(t.ping) ? t.ping + " ms" : "..."),
    "Download: " + (known(t.down) ? t.down + " Mbps" : t.state === "download" && fresh ? speed(t.live) + " Mbps" : "..."),
    "Upload: " + (t.state === "upload" ? speed(t.live) + " Mbps" : "..."),
  ];
  // What the helper last said, when that is recent enough to trust
  if (t.state === "done" && known(t.down) && known(t.up)) {
    // Finished: show the result at once, while the history loads
    return screen({ l1: "Just now", l1Color: GREY, r1: known(t.ping) ? Math.round(t.ping) + " ms" : "", down: t.down, up: t.up, back: back });
  }
  if (t.state === "done" || t.state === "idle") return screen({ l1: "Loading...", l1Color: GREY, back: back });
  if (t.state === "upload") {
    return screen({ l1: "Upload", l1Color: BLUE, big: speed(t.live), unit: "Mbps", bar: t.progress, barColor: BLUE, back: back });
  }
  if (t.state === "download" && fresh) {
    return screen({ l1: "Download", l1Color: GREEN, big: speed(t.live), unit: "Mbps", bar: t.progress, barColor: GREEN, back: back });
  }
  if (t.state === "ping" && fresh && known(t.progress)) {
    return screen({ l1: "Checking ping", big: "...", bar: t.progress, barColor: WHITE, back: back });
  }
  // Otherwise, what the clock says
  if (elapsed < expect.ping) {
    return screen({ l1: "Checking ping", big: "...", bar: elapsed / expect.ping, barColor: WHITE, back: back });
  }
  back[2] = "Download: measuring...";
  screen({ l1: "Download", l1Color: GREEN, big: "Measuring...", bigFont: "small", bigColor: GREY,
    bar: Math.min(0.97, (elapsed - expect.ping) / expect.download), barColor: GREEN, back: back });
}

function renderPage() {
  if (!stats || !stats.count) {
    return screen({ l1: "No tests yet", big: "Press start", bigFont: "small", bigColor: GREY,
      back: ["Net Speed", "No tests yet.", "Press Start to test your", "internet speed."] });
  }
  const p = PAGES[page];
  const d = stats[p.key];
  const tag = demo ? " (demo)" : "";
  if (p.key === "last") {
    return screen({ l1: ago(d.age), l1Color: GREY, r1: Math.round(d.ping) + " ms", down: d.down, up: d.up,
      back: ["Last test, " + ago(d.age).toLowerCase() + tag, "Download: " + d.down.toFixed(1) + " Mbps", "Upload: " + d.up.toFixed(1) + " Mbps",
        "Ping: " + d.ping + " ms" + (known(d.jitter) ? ", jitter " + d.jitter + " ms" : ""), d.server ? "Server: " + d.server : tests(stats.count) + " saved"] });
  }
  if (p.key === "best" || p.key === "worst") {
    return screen({ l1: p.title, l1Color: GOLD, r1: ago(d.age), down: d.down, up: d.up,
      back: [p.title + " in 30 days" + tag, "Download: " + d.down.toFixed(1) + " Mbps", "Upload: " + d.up.toFixed(1) + " Mbps",
        "Ping: " + d.ping + " ms", ago(d.age)] });
  }
  if (!d.n) {
    return screen({ l1: p.title, l1Color: GOLD, big: "No tests", bigFont: "small", bigColor: GREY,
      back: ["Last " + p.title, "No tests in this time.", "Press Start to run one."] });
  }
  screen({ l1: p.title, l1Color: GOLD, r1: tests(d.n), down: d.down, up: d.up,
    back: ["Last " + p.title + ", average" + tag, "Download: " + d.down.toFixed(1) + " Mbps", "Upload: " + d.up.toFixed(1) + " Mbps",
      "Ping: " + d.ping + " ms", tests(d.n) + ", " + speed(d.min) + " to " + speed(d.max) + " down"] });
}

// ---------------------------------------------------------------- demo
// Made-up history and a pretend test, to try the app without the helper.

const DEMO_DOWN = 312.4;
const DEMO_UP = 41.8;
const DEMO_PING = 14.2;

function demoStats() {
  return {
    count: 12,
    last: { down: 298.7, up: 40.9, ping: 15.1, jitter: 1.3, server: "Demo", age: 3 * 3600 },
    h24: { n: 3, down: 304.6, up: 41.2, ping: 14.8, min: 291.5, max: 318.2 },
    d7: { n: 9, down: 301.9, up: 40.7, ping: 15.0, min: 262.4, max: 318.2 },
    d30: { n: 12, down: 296.3, up: 40.1, ping: 15.4, min: 204.8, max: 321.6 },
    best: { down: 321.6, up: 42.3, ping: 13.9, age: 9 * 86400 },
    worst: { down: 204.8, up: 36.2, ping: 21.7, age: 16 * 86400 },
  };
}

function startDemo() {
  demo = true;
  stats = demoStats();
  statsAt = Date.now();
  page = 0;
  mode = "idle";
  render();
}

// Where the pretend test is, from the time since it began
function demoStep() {
  const s = (Date.now() - startedAt) / 1000;
  const wobble = 1 + 0.02 * Math.sin(s * 5);
  testAt = Date.now();
  if (s < 1.2) {
    test = { state: "ping", progress: s / 1.2 };
  } else if (s < 7.2) {
    const p = (s - 1.2) / 6;
    test = { state: "download", progress: p, live: DEMO_DOWN * Math.min(1, 0.35 + p * 2) * wobble, ping: DEMO_PING };
  } else if (s < 11.2) {
    const p = (s - 7.2) / 4;
    test = { state: "upload", progress: p, live: DEMO_UP * Math.min(1, 0.5 + p * 2) * wobble, ping: DEMO_PING, down: DEMO_DOWN };
  } else {
    stats.count++;
    stats.last = { down: DEMO_DOWN, up: DEMO_UP, ping: DEMO_PING, jitter: 1.1, server: "Demo", age: 0 };
    stats.h24.n++;
    stats.d7.n++;
    stats.d30.n++;
    statsAt = Date.now();
    page = 0;
    mode = "idle";
  }
}

// ---------------------------------------------------------------- the helper
// Requests to the helper have their own queue, apart from the bar's: one that
// waits on a busy helper must not hold up the drawing.

let helperChain = Promise.resolve();

function helperJson(path) {
  const p = helperChain.then(function () {
    return fetch(helper + path).then(json, function () { return null; });
  });
  helperChain = p.then(function () {}, function () {});
  return p;
}

function running(s) {
  return s && (s.state === "ping" || s.state === "download" || s.state === "upload");
}

function loadStats() {
  return helperJson("/stats").then(function (s) {
    if (stopped) return;
    if (!s) {
      mode = "offline";
      return render();
    }
    stats = s;
    statsAt = Date.now();
    mode = "idle";
    render();
  });
}

// Asks the helper how the test is going, about twice a second, until it ends.
function follow() {
  if (stopped || mode !== "testing") return;
  helperJson("/status").then(function (s) {
    if (stopped || mode !== "testing") return;
    if (!s) {
      if (Date.now() - startedAt > GIVE_UP_MS) {
        mode = "offline";
        return render();
      }
      return setTimeout(follow, 400);
    }
    test = s;
    testAt = Date.now();
    if (s.state === "done" || s.state === "idle") {
      page = 0;
      return loadStats();
    }
    if (s.state === "error") {
      mode = "failed";
      problem = s.error || "";
      return render();
    }
    render();
    setTimeout(follow, 400);
  });
}

function runTest() {
  mode = "testing";
  test = { state: "ping" };
  testAt = 0;
  startedAt = Date.now();
  if (demo) {
    expect = { ping: 1.2, download: 6, quiet: false };
    return render();   // the ticker below drives the pretend test
  }
  render();
  helperJson("/run").then(function (s) {
    if (stopped) return;
    if (!s) {
      mode = "offline";
      return render();
    }
    if (s.state === "error") {
      mode = "failed";
      problem = s.error || "";
      return render();
    }
    if (s.expect) expect = s.expect;
    test = s;
    testAt = Date.now();
    render();
    // A helper that can't be reached while it downloads: don't ask until the
    // ping and download parts should be over.
    const wait = expect.quiet ? (expect.ping + expect.download) * 1000 - (Date.now() - startedAt) : 0;
    setTimeout(follow, Math.max(0, wait));
  });
}

function address(typed) {
  let a = String(typed || "").replace(/\s+/g, "").replace(/\/+$/, "");
  if (!a) return null;
  if (a.indexOf("://") < 0) a = "http://" + a;
  if (a.replace("://", "").indexOf(":") < 0) a += ":" + HELPER_PORT;
  return a;
}

// Finds the helper, then picks up a test already under way or shows the
// history. "quietly" is the re-check from the setup screen: no Loading screen.
function connect(quietly) {
  if (!quietly) {
    mode = "loading";
    render();
  }
  barJson("apps/settings?app_id=" + APP).then(function (saved) {
    if (stopped) return;
    if (saved && saved.values) for (const k in saved.values) settings[k] = saved.values[k];
    if (settings.demo) return startDemo();
    const typed = address(settings.helper);
    return (typed ? Promise.resolve({ url: typed }) : barJson("storage/read?path=/ext/data/helper.json")).then(function (found) {
      if (stopped || demo) return;
      if (!found || !found.url) {
        mode = "setup";
        render();
        // The helper may be started any moment: look again every few seconds
        setTimeout(function () { if (!stopped && !demo && mode === "setup") connect(true); }, 5000);
        return;
      }
      helper = found.url;
      helperJson("/status").then(function (s) {
        if (stopped || demo) return;
        if (!s) {
          mode = "offline";
          return render();
        }
        if (running(s)) {
          mode = "testing";
          test = s;
          testAt = Date.now();
          if (s.expect) expect = s.expect;
          startedAt = Date.now() - (s.elapsed || 0) * 1000;
          render();
          return follow();
        }
        loadStats();
      });
    });
  });
}

// ---------------------------------------------------------------- input and timers

function turn(by) {
  if (mode !== "idle" || !stats || !stats.count) return;
  page = (page + by + PAGES.length) % PAGES.length;
  render();
}

const unbind = listen("input", function (event) {
  if (stopped) return;
  if (event.key === "back" && event.action === "short") {
    stopped = true;
    for (let i = 0; i < timers.length; i++) clearInterval(timers[i]);
    setTimeout(unbind, 10);
  } else if (event.key === "encoder") {
    turn(event.delta > 0 ? 1 : -1);
  } else if (event.key === "start" && event.action === "short") {
    if (mode === "setup") startDemo();
    else if (mode === "idle" || mode === "failed") runTest();
    else if (mode === "offline") connect();
  }
});

// Moves a test along by the clock, keeps "5m ago" current, and sends a tiny
// off-screen draw every 2 s so a notification that covered the screen is
// noticed and the screen comes back.
timers.push(setInterval(function () {
  if (stopped || mode !== "testing") return;
  if (demo) demoStep();
  render();
}, 300));
timers.push(setInterval(function () { if (!stopped && mode === "idle") render(); }, 30000));
timers.push(setInterval(function () {
  if (!stopped) sendDraw([{ id: "hb", type: "rectangle", x: OFF, y: 0, width: 1, height: 1, fill: "none", border_width: 0, z_index: 0 }]);
}, 2000));

render();
connect();
