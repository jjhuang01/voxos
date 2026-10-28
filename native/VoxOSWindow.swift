// VoxOS 胶囊窗口（原生版）。
//
// 为什么不用 `open -na "Google Chrome" --app=…`：
// 冷启动一次 Chrome 要 1.2–1.5s、常驻 300MB+，而这两件事恰好是用户抱怨的
// 「打开半天打不开」和「内存高」的主因（数字见 docs/PERFORMANCE.md）。
// 一个几十行的 WKWebView 宿主把这两笔账一次划掉：进程启动 ~60ms、常驻 ~60MB。
//
// 只做三件事：开一个窗口、把 URL 装进去、窗口关了就把进程退掉。
// 不做托盘、不做菜单栏常驻、不做自动更新——那些是「用完即走」的反面。
//
// 编译（见 scripts/build_app.sh）：
//   swiftc -O -o build/VoxOSWindow native/VoxOSWindow.swift \
//          -framework Cocoa -framework WebKit

import Cocoa
import WebKit

struct Options {
    var url = URL(string: "http://127.0.0.1:8765/")!
    var width: CGFloat = 420
    var height: CGFloat = 620

    static func parse(_ argv: [String]) -> Options {
        var options = Options()
        var index = 1
        while index < argv.count {
            let key = argv[index]
            let value = index + 1 < argv.count ? argv[index + 1] : ""
            switch key {
            case "--url":
                if let parsed = URL(string: value) { options.url = parsed }
                index += 2
            case "--width":
                options.width = CGFloat(Double(value) ?? 420)
                index += 2
            case "--height":
                options.height = CGFloat(Double(value) ?? 620)
                index += 2
            default:
                index += 1
            }
        }
        return options
    }
}

final class PillWindow: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private let options: Options
    private var attempts = 0
    private var showedStarting = false
    private let started = Date()

    init(options: Options) {
        self.options = options
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        // 窗口先出来，内容后到：服务还在起的时候显示一句话，而不是让用户对着
        // 一片空白猜「是不是又没打开」。这是「秒开」的全部含义——
        // 用户感知的是「有东西立刻出现在我点的地方」，不是「数据到了」。
        // 直接连：服务在跑时这就是最快路径（~350ms 出内容），不要为了
        // 「先显示一句话」白白多等一帧。连不上才退到「正在启动…」。
        loadWithRetry()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // 窗口关了就等于「用完了」：进程跟着退，不留一个看不见的常驻。
        return true
    }

    private func loadWithRetry() {
        attempts += 1
        web.load(URLRequest(url: options.url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func showStarting() {
        render("正在启动…", detail: "VoxOS 服务就绪后会自动连上")
    }

    private func render(_ title: String, detail: String) {
        let html = """
        <html><body style="font:13px -apple-system;padding:28px;color:#8a8a8e;background:transparent">
        <p style="font-size:15px;color:#c8c8cc;margin:0 0 6px">\(title)</p>
        <p style="margin:0">\(detail)</p>
        </body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    /// 最小菜单。没有它 ⌘Q / ⌘W 都不响应——accessory 应用不会自动获得标准菜单。
    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)),
                        keyEquivalent: "w")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 VoxOS", action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        NSApp.mainMenu = main
    }

    private func buildWindow() {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        // 右上角落位：和 macOS 的通知中心同一侧，用户的视线习惯已经在那儿了。
        let origin = NSPoint(x: screen.maxX - options.width - 24,
                             y: screen.maxY - options.height - 24)
        let frame = NSRect(origin: origin, size: NSSize(width: options.width, height: options.height))

        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                      .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "VoxOS"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = true
        window.isReleasedWhenClosed = false
        // 跟随用户切换桌面：语音助手不该只活在某个 Space 里。
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.backgroundColor = .windowBackgroundColor

        let configuration = WKWebViewConfiguration()
        configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: frame, configuration: configuration)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")   // 让页面自己的圆角透出来
        window.contentView = web
        window.makeKeyAndOrderFront(nil)
    }

    /// 页面里 window.open 的链接改在当前窗口打开：这个窗口没有标签页概念。
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigationAction.targetFrame == nil {
            webView.load(navigationAction.request)
        }
        return nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        // 服务还没起来是**正常状态**，不是错误：启动器可能比守护进程先跑。
        // 每 150ms 重试一次，最多 8 秒；超时才认输，把地址写出来让人能自己查。
        if Date().timeIntervalSince(started) > 8 {
            render("没能连上 VoxOS", detail: options.url.absoluteString)
            return
        }
        if !showedStarting {
            showedStarting = true
            showStarting()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            self?.loadWithRetry()
        }
    }
}

let options = Options.parse(CommandLine.arguments)
let app = NSApplication.shared
// accessory：不占 Dock、不抢菜单栏。语音胶囊是「用完即走」的，不是第二个常驻 App。
app.setActivationPolicy(.accessory)
let delegate = PillWindow(options: options)
app.delegate = delegate
app.run()
