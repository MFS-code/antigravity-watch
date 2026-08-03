// banglebridge — minimal CoreBluetooth CLI for talking to a Bangle.js watch.
//
//   banglebridge scan [seconds]        list advertising BLE devices
//   banglebridge send <namePrefix>     read JS payload from stdin, write it to
//                                      the watch's Nordic UART RX characteristic
//
// Built as a plain CLI with NSBluetoothAlwaysUsageDescription embedded in a
// __info_plist section so TCC can attribute Bluetooth use to this binary.

import CoreBluetooth
import Foundation

let NUS_SERVICE = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
let NUS_RX = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
let NUS_TX = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")

var logPath: String? = nil

func log(_ msg: String) {
    print(msg)
    if let p = logPath {
        let line = msg + "\n"
        if let fh = FileHandle(forWritingAtPath: p) {
            fh.seekToEndOfFile()
            fh.write(line.data(using: .utf8)!)
            fh.closeFile()
        } else {
            try? line.write(toFile: p, atomically: true, encoding: .utf8)
        }
    }
}

func fail(_ msg: String) -> Never {
    log("ERROR: " + msg)
    exit(1)
}

final class Bridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    let mode: String
    let namePrefix: String
    let scanSeconds: Double
    let payload: Data
    var fifoPath: String? = nil
    var central: CBCentralManager!
    var target: CBPeripheral?
    var rxChar: CBCharacteristic?
    var seen = Set<String>()
    var outbox = Data()          // daemon: bytes queued for the watch
    var pumpStarted = false
    var fifoStarted = false
    var pauseTicks = 0           // extra pump ticks to wait after a newline
    var pumped = 0               // total bytes written to the watch
    var rxBuf = ""               // text coming back from the watch

    init(mode: String, namePrefix: String, scanSeconds: Double, payload: Data) {
        self.mode = mode
        self.namePrefix = namePrefix
        self.scanSeconds = scanSeconds
        self.payload = payload
        super.init()
        self.central = CBCentralManager(delegate: self, queue: nil)
    }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        switch c.state {
        case .poweredOn:
            log("STATE: poweredOn — scanning...")
            c.scanForPeripherals(withServices: nil, options: nil)
            if mode == "daemon" {   // accept FIFO input immediately; outbox
                startPump()          // drains once the watch is connected
                startFifoReader()
            }
            if mode == "scan" {
                DispatchQueue.main.asyncAfter(deadline: .now() + scanSeconds) {
                    log("SCAN_DONE: \(self.seen.count) named devices")
                    exit(0)
                }
            } else if mode == "send" {
                DispatchQueue.main.asyncAfter(deadline: .now() + scanSeconds) {
                    if self.target == nil {
                        fail("no device with name prefix \"\(self.namePrefix)\" found in \(Int(self.scanSeconds))s (is the watch advertising / disconnected from other hosts?)")
                    }
                }
            } // daemon: scan until found, forever
        case .unauthorized:
            fail("Bluetooth permission denied (TCC). Allow this tool in System Settings > Privacy & Security > Bluetooth.")
        case .poweredOff:
            fail("Bluetooth is powered off")
        case .unsupported:
            fail("Bluetooth unsupported on this machine")
        default:
            break // resetting/unknown: wait for next state
        }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral,
                        advertisementData ad: [String: Any], rssi: NSNumber) {
        // advertised name first: p.name is a stale system cache and masks
        // name changes the watch makes (e.g. our verification beacons)
        let name = (ad[CBAdvertisementDataLocalNameKey] as? String) ?? p.name ?? ""
        if !name.isEmpty && !seen.contains(name) {
            seen.insert(name)
            log("DEVICE: \(name)  rssi=\(rssi)")
        }
        if mode != "scan", target == nil, !namePrefix.isEmpty, name.hasPrefix(namePrefix) {
            target = p
            log("FOUND: \(name) — connecting...")
            c.stopScan()
            c.connect(p, options: nil)
        }
    }

    func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        if mode == "daemon" {
            log("DISCONNECTED — rescanning...")
            target = nil
            rxChar = nil
            seen.removeAll()
            c.scanForPeripherals(withServices: nil, options: nil)
        } else {
            fail("disconnected: \(error?.localizedDescription ?? "")")
        }
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
        log("CONNECTED: \(p.name ?? "?")")
        p.delegate = self
        p.discoverServices(nil)   // discover everything so we can audit the table
    }

    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        fail("connect failed: \(error?.localizedDescription ?? "unknown")")
    }

    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        for svc in p.services ?? [] { log("SERVICE: \(svc.uuid.uuidString)") }
        guard error == nil, let s = p.services?.first(where: { $0.uuid == NUS_SERVICE }) else {
            fail("Nordic UART service not found: \(error?.localizedDescription ?? "")")
        }
        p.discoverCharacteristics(nil, for: s)
    }

    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor ch: CBCharacteristic, error: Error?) {
        log("NOTIFY-STATE: \(ch.uuid == NUS_TX ? "TX" : ch.uuid.uuidString) on=\(ch.isNotifying) err=\(error?.localizedDescription ?? "none")")
    }

    func peripheral(_ p: CBPeripheral, didUpdateValueFor ch: CBCharacteristic, error: Error?) {
        guard ch.uuid == NUS_TX, let d = ch.value,
              let s = String(data: d, encoding: .utf8) else { return }
        rxBuf += s
        while let nl = rxBuf.firstIndex(of: "\n") {
            let line = String(rxBuf[..<nl]).trimmingCharacters(in: .whitespaces)
            rxBuf = String(rxBuf[rxBuf.index(after: nl)...])
            if !line.isEmpty { log("WATCH: \(line)") }
        }
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
        for c in s.characteristics ?? [] {
            log("CHAR: \(c.uuid.uuidString) props=\(c.properties.rawValue)")
        }
        guard error == nil, let ch = s.characteristics?.first(where: { $0.uuid == NUS_RX }) else {
            fail("UART RX characteristic not found: \(error?.localizedDescription ?? "")")
        }
        rxChar = ch
        if let tx = s.characteristics?.first(where: { $0.uuid == NUS_TX }) {
            p.setNotifyValue(true, for: tx)
            log("TX-SUBSCRIBED")
        } else {
            log("TX-NOT-FOUND: chars=\(s.characteristics?.map { $0.uuid.uuidString } ?? [])")
        }
        if mode == "daemon" {
            log("READY: \(target?.name ?? "?")")
            startPump()
            startFifoReader()
        } else {
            log("WRITING: \(payload.count) bytes")
            writeChunks(p: p, ch: ch, offset: 0)
        }
    }

    // daemon: drain outbox with acknowledged writes — the watch's response
    // paces us (real flow control) and errors are visible
    var writing = false

    func startPump() {
        if pumpStarted { return }
        pumpStarted = true
        let t = Timer(timeInterval: 0.025, repeats: true) { _ in self.kick() }
        RunLoop.main.add(t, forMode: .default)
    }

    func kick() {
        guard !writing, let p = target, let ch = rxChar, !outbox.isEmpty else { return }
        if pauseTicks > 0 { pauseTicks -= 1; return }
        let n = min(20, outbox.count)
        let chunk = outbox.prefix(n)
        writing = true
        p.writeValue(chunk, for: ch, type: .withResponse)
        outbox.removeFirst(n)
        pumped += n
        // breathe after each full line so the watch can run it
        // (flash writes during app installs take real time)
        if chunk.contains(0x0A) { pauseTicks = 4 }
    }

    func peripheral(_ p: CBPeripheral, didWriteValueFor ch: CBCharacteristic, error: Error?) {
        writing = false
        if let e = error { log("WRITE-ERR: \(e.localizedDescription)") }
        else if outbox.isEmpty { log("PUMPED: \(pumped) bytes total") }
    }

    // daemon: forward every byte written to the FIFO into the outbox
    func startFifoReader() {
        if fifoStarted { return }
        fifoStarted = true
        guard let path = fifoPath else { fail("daemon needs --cmd <fifo>") }
        Thread.detachNewThread {
            while true {
                let fd = open(path, O_RDONLY)   // blocks until a writer appears
                if fd < 0 { Thread.sleep(forTimeInterval: 1); continue }
                var buf = [UInt8](repeating: 0, count: 4096)
                while true {
                    let n = read(fd, &buf, buf.count)
                    if n <= 0 { break }          // writer closed; reopen
                    let data = Data(buf[0..<n])
                    DispatchQueue.main.async { self.outbox.append(data) }
                }
                close(fd)
            }
        }
    }

    func writeChunks(p: CBPeripheral, ch: CBCharacteristic, offset: Int) {
        if offset >= payload.count {
            // give the last write time to flush before disconnecting
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                self.central.cancelPeripheralConnection(p)
                log("SENT: ok")
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { exit(0) }
            }
            return
        }
        let end = min(offset + 20, payload.count)
        p.writeValue(payload.subdata(in: offset..<end), for: ch, type: .withoutResponse)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.02) {
            self.writeChunks(p: p, ch: ch, offset: end)
        }
    }
}

let args = CommandLine.arguments
var rest: [String] = Array(args.dropFirst())
if let i = rest.firstIndex(of: "--log"), i + 1 < rest.count {
    logPath = rest[i + 1]
    try? "".write(toFile: rest[i + 1], atomically: true, encoding: .utf8)
    rest.removeSubrange(i...(i + 1))
}
let mode = rest.count > 0 ? rest[0] : "scan"
var bridge: Bridge

switch mode {
case "scan":
    let secs = rest.count > 1 ? (Double(rest[1]) ?? 8) : 8
    bridge = Bridge(mode: "scan", namePrefix: "", scanSeconds: secs, payload: Data())
case "send":
    guard rest.count > 1 else { fail("usage: banglebridge [--log f] send <namePrefix> [payloadFile]  (default stdin)") }
    let payload: Data
    if rest.count > 2 {
        guard let d = FileManager.default.contents(atPath: rest[2]) else { fail("cannot read payload file \(rest[2])") }
        payload = d
    } else {
        payload = FileHandle.standardInput.readDataToEndOfFile()
    }
    guard !payload.isEmpty else { fail("empty payload") }
    bridge = Bridge(mode: "send", namePrefix: rest[1], scanSeconds: 15, payload: payload)
case "daemon":
    guard rest.count > 1 else { fail("usage: banglebridge [--log f] daemon <namePrefix> --cmd <fifo>") }
    var fifo: String? = nil
    if let i = rest.firstIndex(of: "--cmd"), i + 1 < rest.count { fifo = rest[i + 1] }
    guard let f = fifo else { fail("daemon needs --cmd <fifoPath>") }
    bridge = Bridge(mode: "daemon", namePrefix: rest[1], scanSeconds: 0, payload: Data())
    bridge.fifoPath = f
default:
    fail("unknown mode \(mode); use scan|send|daemon")
}

withExtendedLifetime(bridge) { RunLoop.main.run() }
