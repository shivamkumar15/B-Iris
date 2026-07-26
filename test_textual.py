from textual.app import App
class TestApp(App):
    def watch_theme(self, new_theme: str) -> None:
        print(f"Theme changed to {new_theme}")
