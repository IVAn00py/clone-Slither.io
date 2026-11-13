import webview

def main():
    webview.create_window(
        "Slither",
        "game.html",
        width=1280,
        height=720,
        resizable=True,
    )
    webview.start()

if __name__ == "__main__":
    main()