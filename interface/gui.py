import sys
import paho.mqtt.client as mqtt
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget, QTextEdit, QVBoxLayout, QAction, QMainWindow, QFrame, QHBoxLayout, QLabel

class MQTTClientThread(QThread):
    messageReceived = pyqtSignal(str)

    def __init__(self, host, port, topic):
        QThread.__init__(self)
        self.host = host
        self.port = port
        self.topic = topic

    def run(self):
        client = mqtt.Client()
        client.connect("localhost", 1883, 60)
        client.subscribe(self.topic)

        def on_message(client, userdata, message):
            self.messageReceived.emit(message.payload.decode())

        client.on_message = on_message
        client.loop_forever()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("OWL Cab GUI")
        self.setGeometry(100, 100, 400, 400)

        layout = QVBoxLayout()
        self.colour_frames = {}

        self.textEdit = QTextEdit()
        self.textEdit.setReadOnly(True)
        layout.addWidget(self.textEdit)

        thread = MQTTClientThread("localhost", 1883, topic='detection')
        thread.messageReceived.connect(self.on_message_received)
        thread.start()
        vis_layout = QHBoxLayout()

        for i in range(4):
            nozzle_layout = QVBoxLayout()
            nozzle_label = QLabel(f"Nozzle {i}")
            nozzle_layout.addWidget(nozzle_label)

            colour_frame = QFrame()
            colour_frame.setMinimumHeight(30)
            colour_frame.setStyleSheet("background-color: gray")
            self.colour_frames[i] = colour_frame
            nozzle_layout.addWidget(colour_frame)
            vis_layout.addLayout(nozzle_layout)
        layout.addLayout(vis_layout)

        centralWidget = QWidget()
        centralWidget.setLayout(layout)
        self.setCentralWidget(centralWidget)

        exitAction = QAction("Exit", self)
        exitAction.setShortcut("Ctrl+Q")
        exitAction.triggered.connect(self.cleanup)
        self.addAction(exitAction)

    def on_message_received(self, message):
        self.textEdit.append(message)
        message_list = message.split(', ')
        nozzle = int(message_list[0])
        status = message_list[1]
        # print(message_list)
        if status == "'on'":
            self.colour_frames[nozzle].setStyleSheet("background-color: green")
        else:
            self.colour_frames[nozzle].setStyleSheet("background-color: gray")


    def cleanup(self):
        for thread in self.findChildren(MQTTClientThread):
            thread.stop()
            thread.wait()

        self.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    mainWindow = MainWindow()
    mainWindow.show()
    sys.exit(app.exec_())
