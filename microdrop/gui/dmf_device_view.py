"""
Copyright 2011 Ryan Fobel

This file is part of Microdrop.

Microdrop is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Microdrop is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Microdrop.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import division
from collections import namedtuple
from datetime import datetime
import time
import traceback
from math import pi
import os

import gtk
import cairo
import numpy as np
import yaml
import gst

from .warp_perspective import warp_perspective, WarpBin
from .rated_bin import RatedBin
from .cairo_draw import CairoDrawBase
from .gstreamer_view import GStreamerVideoView
from pygtkhelpers.utils import gsignal
from pygtkhelpers.delegates import SlaveView
from app_context import get_app
from opencv.safe_cv import cv
from opencv.registration_dialog import RegistrationDialog
from utility.gui import text_entry_dialog
from logger import logger
import app_state


Dims = namedtuple('Dims', 'x y width height')

class ElectrodeContextMenu(SlaveView):
    '''
    Slave view for context-menu for an electrode in the DMF device
    view.

    The signal 'registration-request' is triggered when registration is
    selected from the menu.
    '''
    builder_file = 'right_click_popup.glade'

    gsignal('registration-request')

    def disable_video_background(self):
        app = get_app()
        self.last_frame = None
        self.background = None

    def on_edit_electrode_channels__activate(self, widget, data=None):
        # TODO: set default value
        channel_list = ','.join([str(i) for i in self.last_electrode_clicked.channels])
        app = get_app()
        channel_list = text_entry_dialog('Channels', channel_list, 'Edit electrode channels')
        if channel_list:
            channels = channel_list.split(',')
            #try: # convert to integers
            if len(channels[0]):
                for i in range(0,len(channels)):
                    channels[i] = int(channels[i])
            else:
                channels = []
            if channels and max(channels) >= len(self.state_of_channels):
                # zero-pad channel states for all steps
                for i in range(len(app.protocol)):
                    self.state_of_channels[:] = \
                        np.concatenate([self.state_of_channels,
                        np.zeros(max(channels) - \
                        len(self.state_of_channels)+1, int)])
            self.last_electrode_clicked.channels = channels
            app.state.trigger_event(app_state.DEVICE_CHANGED)
            #except:
                #logger.error("Invalid channel.")
        
    def on_edit_electrode_area__activate(self, widget, data=None):
        app = get_app()
        if app.dmf_device.scale is None:
            area = ""
        else:
            area = self.last_electrode_clicked.area() * app.dmf_device.scale
        area = text_entry_dialog("Area of electrode in mm<span "
                "rise=\"5000\" font_size=\"smaller\">2</span>:", str(area),
                        "Edit electrode area")
        if area:
            if is_float(area):
                app.dmf_device.scale = \
                    float(area)/self.last_electrode_clicked.area()
            else:
                logger.error("Area value is invalid.")

    def on_register_device__activate(self, widget, data=None):
        self.emit('registration-request')

    def popup(self, state_of_channels, electrode, button, time):
        self.last_electrode_clicked = electrode
        self.state_of_channels = state_of_channels
        self.menu_popup.popup(None, None, None, button, time, None)

    def add_item(self, menu_item):
        self.menu_popup.append(menu_item)
        menu_item.show()


class DmfDeviceView(GStreamerVideoView):
    '''
    Slave view for DMF device view.

    This view contains a canvas where video is overlayed with a
    graphical rendering of the device.  The video can optionally be
    registered to align it to the device rendering.  The signal
    'transform-changed' is emitted whenever a video registration has
    been completed.

    The signal 'channel-state-changed' is emitted whenever the state of
    a channel has changed as a result of interaction with the device
    view.
    '''
    builder_file = 'dmf_device_view.glade'

    gsignal('channel-state-changed', object)
    gsignal('transform-changed', object)

    def __init__(self, dmf_device_controller, name):
        self.controller = dmf_device_controller
        self.display_scale = 1
        self.video_scale = 1
        self.last_frame_time = datetime.now()
        self.last_frame = None
        self.display_fps_inv = 0.1
        self.video_offset = (0,0)
        self.display_offset = (0,0)
        self.electrode_color = {}
        self.background = None
        self._transform_matrix = None
        self.overlay_opacity = None
        self.pixmap = None

        self.popup = ElectrodeContextMenu(self)
        self.popup.connect('registration-request', self.on_register)
        self.force_aspect_ratio = False
        self.sink = None
        self.window_xid = None
        SlaveView.__init__(self)
        self.pipeline = self.get_pipeline()

    def grab_frame(self):
        #return self.play_bin.grab_frame()
        return None

    def start_recording(self, video_path):
        if self.controller.video_enabled:
            camera_bin = self.pipeline.get_by_name('camera_bin')
            ready = False
            for i in range(5):
                if camera_bin.get_property('ready-for-capture'):
                    ready = True
                    break
                time.sleep(0.1)
            if not ready:
                raise RuntimeError, 'camerabin is not ready for capture'
            camera_bin.set_property('filename', video_path)
            camera_bin.emit('capture-start')
            logger.info('[DmfDeviceView] recording to: %s' % video_path)

    def stop_recording(self):
        camera_bin = self.pipeline.get_by_name('camera_bin')
        camera_bin.emit('capture-stop')
        if self.controller.video_enabled:
            app = get_app()
            app.experiment_log.add_data({
                    'record_start_timestamp': self.start_time},
                            self.controller.name)

    def get_pipeline(self):
        pipeline = gst.Pipeline('pipeline')

        camera_bin = gst.element_factory_make('camerabin', 'camera_bin')
        warp_bin = WarpBin('warp_bin')

        camera_bin.set_property('viewfinder-filter', warp_bin)

        # Disable audio
        flags = camera_bin.get_property('flags')
        camera_bin.set_property('flags', flags | 0x020)

        # Set mode to video (rather than image)
        camera_bin.set_property('mode', 1)

        # Set video source to test source
        camera_bin.set_property('video-source', self.get_video_src())

        # Set recording format to mpeg4 avi
        avi_mux = gst.element_factory_make('avimux', 'avi_mux')
        ffenc_mpeg4 = gst.element_factory_make('ffenc_mpeg4', 'ffenc_mpeg40') 
        ffenc_mpeg4.set_property('bitrate', 1200000)

        camera_bin.set_property('video-muxer', avi_mux)
        camera_bin.set_property('video-encoder', ffenc_mpeg4)

        pipeline.add(camera_bin)
        clock = pipeline.get_clock()
        clock.set_property('clock-type', 0)

        return pipeline

    def get_video_src(self):
        blank_screen = False
        if not hasattr(self.controller, 'video_enabled') or not\
                self.controller.video_enabled:
            blank_screen = True
        elif not self.controller.video_enabled:
            blank_screen = True

        if blank_screen:
            video_src = gst.element_factory_make('videotestsrc', 'video_src')
            video_src.set_property('pattern', 2)
            return RatedBin('video_src', video_src=video_src)
        else:
            return RatedBin('video_src')

    @property
    def webcam_src(self):
        self._webcam_src = getattr(self, '_webcam_src', None)
        if self._webcam_src is None:
            if os.name == 'nt':
                webcam_src = gst.element_factory_make('dshowvideosrc', 'webcam_src')
                webcam_src.set_property('device-name', 'Microsoft LifeCam Studio')
            else:
                webcam_src = gst.element_factory_make('v4l2src', 'webcam_src')
                #webcam_src.set_property('device', '/dev/video1')
                webcam_src.set_property('device', '/dev/video0')
            self._webcam_src = webcam_src
        return self._webcam_src

    def enable_video(self):
        self.pipeline.set_state(gst.STATE_NULL)
        self.pipeline = self.get_pipeline()
        self.pipeline.set_state(gst.STATE_PLAYING)
        if self.transform_matrix:
           self.transform_matrix = self.transform_matrix
        self.controller.set_overlay()

    def disable_video(self):
        self.pipeline.set_state(gst.STATE_NULL)
        self.pipeline = self.get_pipeline()
        self.pipeline.set_state(gst.STATE_PLAYING)
        self.controller.set_overlay()

    def on_device_area__realize(self, widget, *args):
        self.on_realize(widget)
        self.pipeline.set_state(gst.STATE_PLAYING)

    def on_device_area__size_allocate(self, *args):
        x, y, width, height = self.device_area.get_allocation()
        self.pixmap = gtk.gdk.Pixmap(self.device_area.window, width, height)

    def fit_device(self, video_dims, padding=None):
        app = get_app()
        if app.dmf_device and len(app.dmf_device.electrodes):
            if padding is None:
                padding = 10
            display_dims = Dims(*self.device_area.get_allocation())
            device = Dims(*app.dmf_device.get_bounding_box())
            display_scale_x = (display_dims.width - 2 * padding) / device.width
            display_scale_y = (display_dims.height - 2 * padding) / device.height
            self.display_scale = min(display_scale_x, display_scale_y)
            if display_scale_x < display_scale_y: # center device vertically
                self.display_offset = (-device.x + padding / self.display_scale,
                               -device.y + padding / self.display_scale + \
                               ((display_dims.height - 2 * padding)\
                                        / self.display_scale - device.height) / 2)
            else:  # center device horizontally
                self.display_offset = (-device.x + padding / self.display_scale +  \
                               ((display_dims.width - 2 * padding)\
                                    / self.display_scale - device.width) / 2,
                                - device.y + padding / self.display_scale)
            #self.play_bin.scale(display_dims.width, display_dims.height)

    def _draw_on(self, buf):
        try:
            caps = buf.get_caps()
            width = caps[0]['width']
            height = caps[0]['height']
            video_dims = Dims(0, 0, width, height)
            self.fit_device(video_dims)
            framerate = caps[0]['framerate']
            surface = cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, width, height, 4 * width)
            cairo_context = cairo.Context(surface)
        except:
            print "Failed to create cairo surface for buffer"
            traceback.print_exc()
            return
        try:
            self.draw_on_cairo(cairo_context, alpha=self.overlay_opacity / 100.)
        except:
            print "Failed cairo render"
            traceback.print_exc()

    def draw_on_cairo(self, cr, alpha=1.0):
        app = get_app()
        x, y = self.display_offset
        cr.scale(self.display_scale, self.display_scale)
        cr.translate(x, y)
        if app.dmf_device:
            for id, electrode in app.dmf_device.electrodes.iteritems():
                if self.electrode_color.keys().count(id):
                    r, g, b = self.electrode_color[id]
                    self.draw_electrode(electrode, cr, (b, g, r, alpha))

    def draw_electrode(self, electrode, cr, color=None):
        p = electrode.path
        cr.save()
        if color is None:
            color = [v / 255. for v in p.color]
        if len(color) < 4:
            color += [1.] * (len(color) - 4)
        cr.set_source_rgba(*color)
        for loop in p.loops:
            cr.move_to(*loop.verts[0])
            for v in loop.verts[1:]:
                cr.line_to(*v)
            cr.close_path()
            cr.fill()
        cr.restore()

    def translate_coords(self, x, y):
        translated = (x / self.display_scale - self.display_offset[0], y / self.display_scale - self.display_offset[1])
        return translated

    def get_clicked_electrode(self, event):
        app = get_app()
        shape = app.dmf_device.body_group.space.point_query_first(
                self.translate_coords(*event.get_coords()))
        if shape:
            return app.dmf_device.get_electrode_from_body(shape.body)
        return None

    def on_electrode_click(self, electrode, event):
        app = get_app()
        options = self.controller.get_step_options()
        state = options.state_of_channels
        if event.button == 1:
            if len(electrode.channels): 
                for channel in electrode.channels:
                    if state[channel] > 0:
                        state[channel] = 0
                    else:
                        state[channel] = 1
                self.emit('channel-state-changed', electrode.channels[:])
            else:
                logger.error("no channel assigned to electrode.")
        elif event.button == 3:
            self.popup.popup(state, electrode, event.button, event.time)
        return True

    def on_device_area__key_press_event(self, widget, data=None):
        pass
    
    def on_device_area__button_press_event(self, widget, event):
        '''
        Modifies state of channel based on mouse-click.
        '''
        app = get_app()
        self.widget.grab_focus()
        # Determine which electrode was clicked (if any)
        electrode = self.get_clicked_electrode(event)
        if electrode:
            self.on_electrode_click(electrode, event)
        return True

    # device view events

    #def on_device_area__expose_event(self, widget, event):
        #if self.pixmap:
            #x , y, width, height = event.area
            #widget.window.draw_drawable(widget.get_style().white_gc,
                                        #self.pixmap, x, y, x, y, width, height)
        #return False

    def on_new_frame(self, frame, depth, frame_time):
        app = get_app()
        if not app.dmf_device:
            return
        self.last_frame = frame
        now = datetime.now()
        if (now - self.last_frame_time).total_seconds() < self.display_fps_inv:
            # Wait to respect display FPS.
            return

        self.last_frame_time = now

    def on_message(self, bus, message):
        super(DmfDeviceView, self).on_message(bus, message)
        t = message.type
        if t == gst.MESSAGE_STATE_CHANGED:
            if message.src == self.pipeline and\
                    message.structure['new-state'] == gst.STATE_PLAYING:
                if self.transform_matrix is not None:
                    self.transform_matrix = self.transform_matrix
            else:
                camera_bin = self.pipeline.get_by_name('camera_bin')
                video_encoder = camera_bin.get_property('video-encoder')
                if message.src == video_encoder and\
                        message.structure['new-state'] == gst.STATE_PLAYING:
                    self.start_time = datetime.fromtimestamp(
                        self.pipeline.get_clock().get_time() * 1e-9)

    @property
    def transform_matrix(self):
        return self._transform_matrix

    @transform_matrix.setter
    def transform_matrix(self, transform_matrix):
        self._transform_matrix = transform_matrix
        transform_str = ','.join([str(v)
                for v in transform_matrix.flatten()])
        camera_bin = self.pipeline.get_by_name('camera_bin')
        warp_bin = camera_bin.get_by_name('warp_bin')
        if warp_bin:
            warp_bin.warper.set_property('transform-matrix', transform_str)

    def on_register(self, *args, **kwargs):
        if self.last_frame is None:
            return
        size = self.pixmap.get_size()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, *size)
        cr = cairo.Context(surface)
        self.draw_on_cairo(cr)
        alpha_image = cv.CreateImageHeader(size, cv.IPL_DEPTH_8U, 4)
        device_image = cv.CreateImage(size, cv.IPL_DEPTH_8U, 3)
        cv.SetData(alpha_image, surface.get_data(), 4 * size[0])
        cv.CvtColor(alpha_image, device_image, cv.CV_RGBA2RGB)
        video_image = cv.CreateImage(size, cv.IPL_DEPTH_8U, 3)
        cv.Resize(self.last_frame, video_image)
        dialog = DeviceRegistrationDialog(device_image, video_image)
        results = dialog.run()
        if results:
            self.transform_matrix = results
            array = np.fromstring(results.tostring(), dtype='float32',
                    count=results.width * results.height)
            array.shape = (results.width, results.height)
            self.emit('transform-changed', array)

    
class DeviceRegistrationDialog(RegistrationDialog):
    '''
    This dialog is used to register the video to the DMF device
    rendering.
    '''

    def __init__(self, device_image, video_image, *args, **kwargs):
        super(DeviceRegistrationDialog, self).__init__(*args, **kwargs)
        self.device_image = device_image
        self.video_image = video_image

    def get_glade_path(self):
        from utility import base_path
        return base_path().joinpath('opencv', 'glade', 'registration_demo.glade')

    def get_original_image(self):
        return self.device_image

    def get_rotated_image(self):
        return self.video_image
