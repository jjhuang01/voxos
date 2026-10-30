// VoxOS 胶囊窗口（原生版）。
//
// 为什么不用 `open -na "Google Chrome" --app=…`：
// 冷启动一次 Chrome 要 1.2–1.5s、常驻 300MB+，而这两件事恰好是用户抱怨的
// 「打开半天打不开」和「内存高」的主因（数字见 docs/PERFORMANCE.md）。
// 一个几十行的 WKWebView 宿主把这两笔账一次划掉：进程启动 ~60ms、常驻 ~60MB。
//
// 只做三件事：开一个窗口、把 URL 装进去、窗口关了就把进程退掉。
// 占 Dock 图标（用户要能在 Dock / ⌘Tab 里找到并激活它），不做托盘和自动更新。
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

/// 页面占满整个窗口，WKWebView 会吞掉 mouseDown，`isMovableByWindowBackground`
/// 因此失效。顶部这一条交给窗口拖动，其余照常给页面（按钮、输入框）。
final class DraggableWebView: WKWebView {
    static let dragStrip: CGFloat = 40

    /// 窗口不在前台时第一下就能拖，不用先点一下激活。
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    /// 按下的位置在拖动条里时记下起点；真正拖动要等移动超过阈值。
    /// 以前按下即 performDrag，胶囊上的齿轮、麦克风和展开点击全被吞掉，
    /// 面板用 Esc 关掉后就再也点不开。
    private var dragOrigin: NSPoint?
    private static let dragSlop: CGFloat = 3

    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let fromTop = isFlipped ? point.y : bounds.height - point.y
        dragOrigin = fromTop <= DraggableWebView.dragStrip ? event.locationInWindow : nil
        super.mouseDown(with: event)
    }

    /// 超过阈值后改为逐帧移动窗口。
    ///
    /// 这里不用 `window.performDrag(with:)`：它内部要再跑一轮基于**鼠标按下事件**的
    /// 拖动循环，而我们拿到的是 `mouseDragged`。传进去只在下一次事件到来时才可能生效，
    /// 表现就是「按住不动、动一下才跳一截」。直接改 origin 与事件增量一一对应，
    /// 松手即停，也不依赖 AppKit 私有语义。
    override func mouseDragged(with event: NSEvent) {
        guard let origin = dragOrigin, let window = window else {
            super.mouseDragged(with: event)
            return
        }
        guard hypot(event.locationInWindow.x - origin.x,
                    event.locationInWindow.y - origin.y) > DraggableWebView.dragSlop else {
            super.mouseDragged(with: event)
            return
        }
        var frame = window.frame
        // AppKit 的窗口坐标以左下角为原点、y 向上；鼠标事件的 deltaY 向下为正。
        frame.origin.x += event.deltaX
        frame.origin.y -= event.deltaY
        window.setFrameOrigin(frame.origin)
    }

    override func mouseUp(with event: NSEvent) {
        dragOrigin = nil
        super.mouseUp(with: event)
    }
}

final class PillWindow: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                        WKScriptMessageHandler {
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

    /// 点 Dock 图标：窗口被最小化或藏到后面时拉回前台。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
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

    /// 最小菜单。代码创建的应用不会自动获得标准菜单，没有它 ⌘Q / ⌘W 都不响应。
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
        // 页面知道自己是胶囊还是展开面板，窗口高度只能由它告诉宿主：
        // 实测同一页面收起 84px、展开 656px，写死 620 会在收起时留一大块空盒子。
        configuration.userContentController.add(self, name: "voxosLayout")
        configuration.userContentController.add(self, name: "voxosReload")
        web = DraggableWebView(frame: frame, configuration: configuration)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")   // 让页面自己的圆角透出来
        window.contentView = web
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: - 页面驱动的尺寸与重试

    /// 页面把渲染高度交给宿主；窗口按顶边锚定伸缩，胶囊始终留在用户放它的地方。
    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        switch message.name {
        case "voxosLayout":
            if let height = (message.body as? NSNumber)?.doubleValue {
                resize(to: CGFloat(height))
            }
        case "voxosReload":
            attempts = 0
            showedStarting = false
            loadWithRetry()
        default:
            break
        }
    }

    private func resize(to contentHeight: CGFloat) {
        guard let window else { return }
        let visible = (window.screen ?? NSScreen.main)?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: options.width, height: 900)
        let clamped = min(max(contentHeight, 56), visible.height - 24)
        var frame = window.frame
        guard abs(frame.height - clamped) > 0.5 else { return }
        let top = frame.maxY
        frame.size = NSSize(width: frame.width, height: clamped)
        frame.origin.y = min(max(visible.minY, top - clamped), visible.maxY - clamped)
        window.setFrame(frame, display: true, animate: false)
    }

    /// 连不上时给一条出路：只写一个地址等于让用户自己去开终端。
    private func showFailure() {
        let address = options.url.absoluteString
        let html = """
        <html><body style="font:13px -apple-system;padding:28px;color:#8a8a8e;background:transparent">
        <p style="font-size:15px;color:#c8c8cc;margin:0 0 6px">没能连上 VoxOS</p>
        <p style="margin:0 0 14px">守护进程可能还没起来：<br><code>\(address)</code></p>
        <button onclick="window.webkit.messageHandlers.voxosReload.postMessage(1)"
          style="font:inherit;padding:7px 14px;border-radius:8px;border:1px solid rgba(255,255,255,.16);
                 background:rgba(255,255,255,.08);color:#f2f3f5;cursor:pointer">重新连接</button>
        </body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
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
            showFailure()
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
// regular：出现在 Dock 和 ⌘Tab 里。accessory 会让窗口被盖住后无处可找。
app.setActivationPolicy(.regular)
let delegate = PillWindow(options: options)
app.delegate = delegate
app.run()
