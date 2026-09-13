# -*- coding: utf-8 -*-
"""
摄像头模块 — USB 摄像头 MJPEG 实时视频流
========================================
使用 OpenCV 采集，multipart/x-mixed-replace 推流。
"""
import cv2
from flask import Response

camera = None


def get_camera():
    global camera
    if camera is None:
        camera = cv2.VideoCapture(0)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return camera


def release_camera():
    global camera
    if camera:
        camera.release()
        camera = None


def generate_frames():
    cam = get_camera()
    if not cam.isOpened():
        yield (b'--frame\r\n'
               b'Content-Type: text/plain\r\n\r\nCamera Error\r\n--frame\r\n')
        return

    while True:
        success, frame = cam.read()
        if not success:
            break
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ret:
            continue
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' +
               buffer.tobytes() + b'\r\n')
