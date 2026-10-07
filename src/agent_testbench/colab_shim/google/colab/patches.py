"""cv2_imshow shows the image as a notebook output, so it is kept as an artifact."""


def cv2_imshow(a):
    import cv2
    from IPython.display import Image, display
    ok, png = cv2.imencode(".png", a)
    if not ok:
        raise ValueError("cv2_imshow: could not encode the image")
    display(Image(data=png.tobytes()))
