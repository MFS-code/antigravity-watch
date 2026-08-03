// AgentWatch — Antigravity agent status on your wrist.
// The Mac bridge drives it by calling _AW(state, message) over BLE UART.
// The Mac re-sends the current state every few seconds, so the screen
// converges even if you exit and re-open the app; buzzes only on change.
var st = "";
var msg = "";
var since = Date();
var C = {
  idle:      { bg:"#222222", fg:"#888888", label:"IDLE" },
  working:   { bg:"#003399", fg:"#ffffff", label:"WORKING" },
  waiting:   { bg:"#cc4400", fg:"#ffffff", label:"NEEDS YOU" },
  attention: { bg:"#e6c200", fg:"#000000", label:"NEEDS", label2:"ATTENTION" },
  done:      { bg:"#007700", fg:"#ffffff", label:"DONE" }
};

function pad(n) { return ("0" + n).substr(-2); }

function draw() {
  var c = C[st] || { bg:"#222222", fg:"#888888", label:"WAITING FOR MAC" };
  g.reset().setBgColor(c.bg).clearRect(0, 0, 176, 176);
  g.setColor(c.fg).setFontAlign(0, 0);
  g.setFont("6x8", 2);
  g.drawString("ANTIGRAVITY", 88, 22);
  g.setFont("6x8", st ? 3 : 2);
  if (c.label2) {
    g.drawString(c.label, 88, 62);
    g.drawString(c.label2, 88, 88);
  } else {
    g.drawString(c.label, 88, 74);
  }
  g.setFont("6x8", 1);
  if (msg) {
    var lines = g.wrapString(msg, 164).slice(0, 4);
    lines.forEach(function (l, i) { g.drawString(l, 88, 108 + i * 11); });
  }
  var d = new Date();
  var mins = Math.round((d - since) / 60000);
  g.drawString(pad(d.getHours()) + ":" + pad(d.getMinutes()) +
               (st && st != "idle" ? "  (" + mins + "m in " + st + ")" : ""),
               88, 164);
}

global._AW = function (s, m) {
  var changed = (s !== st);
  if (changed) since = Date();
  st = s;
  msg = m || "";
  if (changed) {
    if (s == "done") {
      Bangle.buzz(600);
    } else if (s == "waiting") {
      Bangle.buzz(150).then(function () {
        setTimeout(function () { Bangle.buzz(150); }, 180);
      });
    } else if (s == "attention") {
      Bangle.buzz(100);
      setTimeout(function () { Bangle.buzz(100); }, 200);
      setTimeout(function () { Bangle.buzz(100); }, 400);
    } else if (s == "working") {
      Bangle.buzz(40);
    }
  }
  draw();
};

// demo mode: screen stays on and unlocked while this app is open
Bangle.setOptions({ lockTimeout: 0, backlightTimeout: 0, lcdPowerTimeout: 0 });
Bangle.setLocked(false);
Bangle.setLCDPower(1);

Bangle.setUI({ mode: "custom", btn: function () { load(); } }); // button exits
setInterval(draw, 30000);
draw();
