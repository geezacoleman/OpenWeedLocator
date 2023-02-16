import sys

import paho.mqtt.client as mqtt
import sys

class OWLPublisher:
    def __init__(self, broker_address, owl_id):
        self.broker_address = broker_address
        self.owl_id = owl_id
        self.client = mqtt.Client()
        if self.client.connect(self.broker_address, 1883, 60) != 0:
            print("Could not connect to MQTT Broker!")
            sys.exit(-1)

    def publish(self, information, topic):
        if topic == 'detection':

            message = f"{information}".encode()
        else:
            message = information
        self.client.publish(topic, message)
        # print("Published detection from Raspberry Pi {}: {}".format(self.owl_id, message))

    def stop(self):
        self.client.disconnect()

class OWLSubscriber:
    def __init__(self, broker_address, topic):
        self.broker_address = broker_address
        self.topic = topic
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        self.client.connect(self.broker_address)
        self.client.subscribe(self.topic)
        self.client.loop_forever()

    def on_connect(self, client, userdata, flags, rc):
        print("Connected with result code " + str(rc))

    def on_message(self, client, userdata, msg):
        detection = msg.payload.decode()
        # print("Detection: {}".format(detection))

if __name__ == "__main__":
    pub = OWLPublisher("localhost", 10)
    message = (10, 23, 35, 'weed')
    topic = 'detection'
    pub.publish(topic=topic, information=message)

