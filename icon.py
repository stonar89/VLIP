from PIL import Image

img = Image.open("icon.png").convert("RGBA")
img.save("icon.ico", format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("Saved icon.ico")