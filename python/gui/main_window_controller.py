import os
import gtk
from hardware.dmf_control_board import DmfControlBoard

class MainWindowController:
    def __init__(self, app, builder, signals):
        self.app = app
        builder.add_from_file(os.path.join("gui",
                                           "glade",
                                           "main_window.glade"))
        self.window = builder.get_object("window")
        self.label_connection_status = builder.get_object("label_connection_status")
        self.label_experiment_id = builder.get_object("label_experiment_id")
        self.checkbutton_realtime_mode = builder.get_object("checkbutton_realtime_mode")

        signals["on_menu_quit_activate"] = self.on_destroy
        signals["on_window_destroy"] = self.on_destroy
        signals["on_checkbutton_realtime_mode_toggled"] = \
                self.on_realtime_mode_toggled

        for i in range(0,31):
            if app.control_board.Connect("COM%d" % i) == DmfControlBoard.RETURN_OK:
                name = app.control_board.name()
                version = app.control_board.hardware_version()
                if name == "Arduino DMF Controller" and version == "1.1":
                    self.label_connection_status.set_text(name + " v" + version)
                    app.control_board.set_series_resistor(1,3)
                    app.control_board.set_series_resistor(0,0)
                    break

    def main(self):
        self.update()
        gtk.main()

    def on_destroy(self, widget, data=None):
        gtk.main_quit()

    def on_realtime_mode_toggled(self, widget, data=None):
        self.update()

    def update(self):
        self.app.realtime_mode = self.checkbutton_realtime_mode.get_active()
        self.app.device_controller.update()
        self.app.protocol_controller.update()
        self.label_experiment_id.set_text("Experiment: %d" % self.app.experiment_log.get_id())
        
        # process all gtk events
        while gtk.events_pending():
            gtk.main_iteration()