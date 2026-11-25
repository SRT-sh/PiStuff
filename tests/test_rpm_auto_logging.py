"""Automatic recording, manual overrides and idle CAN ownership without hardware."""
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from test_vn300_button_logger import logger


class RpmAutoLoggingTests(unittest.TestCase):
    def setUp(self):
        self.clear_events()
        self.controller = logger.EngineRpmLoggingController()
        self.controller.set_source((0x640, False, 'Engine_Speed', 1.0))
        self.patches = contextlib.ExitStack()
        self.patches.enter_context(mock.patch.object(logger, 'engine_rpm_logging', self.controller))
        self.patches.enter_context(mock.patch.object(logger, 'capture_guard', None))
        self.addCleanup(self.patches.close)
        self.addCleanup(self.clear_events)

    @staticmethod
    def clear_events():
        for event in (logger.stop_requested, logger.shutdown_requested, logger.logging_requested,
                      logger.active_session_stop, logger.setup_stream_requested, logger.recording_busy):
            event.clear()

    def rpm(self, value, now, can_id=0x640, channel='Engine_Speed', extended=False):
        self.controller.observe(SimpleNamespace(arbitration_id=can_id, is_extended_id=extended),
                                [({'channel': channel}, value, value)], now=now)

    def test_exact_threshold_starts_and_repeated_samples_keep_same_request(self):
        logger.setup_stream_requested.set()
        self.rpm(249.99, 0)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(250, 1)
        generation = logger.logging_request_generation
        self.assertTrue(logger.logging_requested.is_set())
        self.assertFalse(logger.setup_stream_requested.is_set())
        self.rpm(3000, 2)
        self.assertEqual(generation, logger.logging_request_generation)

    def test_stop_delay_begins_at_first_low_sample_and_does_not_extend(self):
        self.rpm(250, 0)
        logger.begin_acquisition()
        self.rpm(249, 1)
        self.rpm(0, 12)
        self.controller.poll(now=13.499)
        self.assertTrue(logger.logging_requested.is_set())
        self.controller.poll(now=13.5)
        self.assertFalse(logger.logging_requested.is_set())
        self.assertTrue(logger.active_session_stop.is_set())

    def test_recovery_cancels_stop_without_splitting_run(self):
        self.rpm(300, 0)
        generation = logger.logging_request_generation
        self.rpm(0, 1)
        self.rpm(250, 13)
        self.controller.poll(now=13.5)
        self.assertTrue(logger.logging_requested.is_set())
        self.assertEqual(generation, logger.logging_request_generation)
        self.assertFalse(logger.active_session_stop.is_set())
        self.rpm(0, 14)
        self.controller.poll(now=26.499)
        self.assertTrue(logger.logging_requested.is_set())
        self.controller.poll(now=26.5)
        self.assertFalse(logger.logging_requested.is_set())

    def test_silence_invalid_samples_and_other_traffic_do_not_keep_run_alive(self):
        self.rpm(1000, 0)
        self.rpm(9000, 10, channel='Engine_Speed_Limit')
        self.rpm(9000, 10, can_id=0x64A)
        self.rpm(9000, 10, extended=True)
        for value in (float('nan'), float('inf'), -1, True, '1000'):
            self.rpm(value, 10)
        self.controller.poll(now=12.499)
        self.assertTrue(logger.logging_requested.is_set())
        self.controller.poll(now=12.5)
        self.assertFalse(logger.logging_requested.is_set())
        # Fresh RPM after an engine/ECU restart may start a new run.
        self.rpm(250, 13)
        self.assertTrue(logger.logging_requested.is_set())

    def test_manual_stop_is_immediate_and_requires_engine_cycle_to_restart(self):
        self.rpm(1000, 0)
        logger.toggle_logging_request(False)
        self.assertFalse(logger.logging_requested.is_set())
        self.assertTrue(logger.active_session_stop.is_set())
        self.rpm(1000, 1)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(0, 2)
        self.rpm(250, 3)
        self.assertTrue(logger.logging_requested.is_set())

    def test_manual_on_overrides_pending_stop_even_if_already_recording(self):
        self.rpm(1000, 0)
        self.rpm(0, 1)
        logger.toggle_logging_request(True)
        self.controller.poll(now=100)
        self.assertTrue(logger.logging_requested.is_set())
        logger.toggle_logging_request(False)
        self.assertFalse(logger.logging_requested.is_set())

    def test_gpio_changes_override_rpm_but_boot_off_leaves_automation_armed(self):
        switch = SimpleNamespace(is_pressed=False)
        power = SimpleNamespace()
        gpiozero = SimpleNamespace(Button=mock.Mock(side_effect=[switch, power]))
        with mock.patch.dict('sys.modules', {'gpiozero': gpiozero}):
            logger.install_gpio_callbacks(17, 27, 'unused')
        self.rpm(250, 0)
        self.assertTrue(logger.logging_requested.is_set())
        switch.is_pressed = True
        switch.when_pressed(switch)
        self.rpm(0, 1)
        self.controller.poll(now=20)
        self.assertTrue(logger.logging_requested.is_set())
        self.rpm(1000, 20)
        switch.is_pressed = False
        switch.when_released(switch)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(1000, 21)
        self.assertFalse(logger.logging_requested.is_set())

    def test_rpm_restart_during_flush_preserves_old_stop(self):
        self.rpm(250, 0)
        first = logger.begin_acquisition()
        self.rpm(0, 1)
        self.controller.poll(now=13.5)
        self.rpm(250, 14)
        self.assertTrue(logger.active_session_stop.is_set())
        logger.finish_acquisition(*first)
        self.assertTrue(logger.logging_requested.is_set())
        self.assertTrue(logger.begin_acquisition()[0])
        self.assertFalse(logger.active_session_stop.is_set())

    def test_fault_does_not_automatically_retry_while_engine_stays_running(self):
        self.rpm(1000, 0)
        first = logger.begin_acquisition()
        logger.finish_acquisition(*first)
        self.rpm(1000, 1)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(0, 2)
        self.rpm(250, 3)
        self.assertTrue(logger.logging_requested.is_set())

    def test_configured_threshold_and_delay_including_zero_delay(self):
        self.controller.threshold_rpm = 500
        self.controller.stop_delay_s = 2.5
        self.rpm(499, 0)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(500, 1)
        self.rpm(0, 2)
        self.controller.poll(now=4.499)
        self.assertTrue(logger.logging_requested.is_set())
        self.controller.poll(now=4.5)
        self.assertFalse(logger.logging_requested.is_set())
        self.controller.stop_delay_s = 0
        self.rpm(500, 5)
        self.controller.poll(now=5.1)
        self.assertTrue(logger.logging_requested.is_set())
        self.rpm(0, 5.2)
        self.controller.poll(now=5.2)
        self.assertFalse(logger.logging_requested.is_set())

    def test_shutdown_prevents_rpm_restart(self):
        logger.stop_requested.set()
        self.rpm(1000, 0)
        self.controller.poll(now=20)
        self.assertFalse(logger.logging_requested.is_set())

    def test_angular_units_are_compared_in_rpm(self):
        self.controller.set_source((0x640, False, 'Engine_Speed', 1 / 6))
        self.rpm(1499, 0)
        self.assertFalse(logger.logging_requested.is_set())
        self.rpm(1500, 1)
        self.assertTrue(logger.logging_requested.is_set())

    def test_idle_receiver_decodes_real_csv_rpm_to_start_without_files(self):
        stop = threading.Event()
        msg = SimpleNamespace(arbitration_id=0x640, is_extended_id=False,
                              data=bytes.fromhex('00 FA 00 00 00 00 00 00'))
        bus = mock.Mock()

        def receive(timeout):
            stop.set()
            return msg

        bus.recv.side_effect = receive
        with mock.patch.object(logger, 'can', SimpleNamespace(Bus=mock.Mock(return_value=bus))):
            logger.run_can_logger({'enabled': True, 'interface': 'virtual', 'channel': 'rpm-test',
                                   'signal_map': logger.DEFAULT_CAN_SIGNAL_MAP},
                                  0, stop)
        self.assertTrue(logger.logging_requested.is_set())
        bus.shutdown.assert_called_once()


class RpmSourceAndSettingsTests(unittest.TestCase):
    def test_default_map_prefers_engine_speed_over_vcs_and_limits(self):
        mapping = logger.load_can_signal_map(logger.DEFAULT_CAN_SIGNAL_MAP)
        self.assertEqual(logger.find_engine_rpm_source(None, mapping), (0x640, False, 'Engine_Speed', 1.0))

    def test_custom_csv_id_and_dbc_units(self):
        mapping = {0x123: [{'channel': 'Engine_RPM', 'unit': 'rpm', 'extended': False}],
                   0x124: [{'channel': 'Engine_Speed_Limit', 'unit': 'rpm'}]}
        self.assertEqual(logger.find_engine_rpm_source(None, mapping), (0x123, False, 'Engine_RPM', 1.0))
        database = SimpleNamespace(messages=[SimpleNamespace(frame_id=0x12345, is_extended_frame=True,
                     signals=[SimpleNamespace(name='Engine Speed', unit='deg/s')])])
        self.assertEqual(logger.find_engine_rpm_source(database, {}), (0x12345, True, 'Engine Speed', 1 / 6))
        for mapping in ({}, {0x124: [{'channel': 'Engine_Speed_Limit', 'unit': 'rpm'}]},
                        {0x123: [{'channel': 'Engine_RPM', 'unit': 'km/h'}]}):
            self.assertIsNone(logger.find_engine_rpm_source(None, mapping))

    def test_settings_round_trip_disabled_and_command_line_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text(json.dumps({'can_auto_log_enable': False, 'can_auto_log_rpm': 500,
                                        'can_auto_log_stop_delay': 5.5}))
            parser = logger.settings_parser()
            parser.set_defaults(**logger.load_settings(path, parser))
            args = parser.parse_args([])
            self.assertFalse(args.can_auto_log_enable)
            self.assertEqual((args.can_auto_log_rpm, args.can_auto_log_stop_delay), (500, 5.5))
            self.assertTrue(parser.parse_args(['--can-auto-log-enable']).can_auto_log_enable)
            self.assertFalse(logger.settings_parser().parse_args(['--no-can-auto-log-enable']).can_auto_log_enable)
            path.write_text(json.dumps({'can_auto_log_enable': 'false'}))
            with self.assertRaises(ValueError):
                logger.load_settings(path, logger.settings_parser())

    def test_settings_validation(self):
        for rpm, delay in ((0, 12.5), (-250, 12.5), (float('nan'), 12.5),
                           (250, -1), (250, float('inf')), (None, 12.5), (250, None)):
            with self.subTest(rpm=rpm, delay=delay), self.assertRaises(ValueError):
                logger.EngineRpmLoggingController(rpm, delay)

    def test_main_only_enables_controller_when_can_and_automation_are_enabled(self):
        for can_enabled, auto_enabled in ((False, True), (True, False), (True, True)):
            with self.subTest(can=can_enabled, automatic=auto_enabled), tempfile.TemporaryDirectory() as directory:
                logger.stop_requested.clear()
                config = Path(directory) / 'settings.json'
                config.write_text(json.dumps({'can_enable': can_enabled, 'can_auto_log_enable': auto_enabled,
                                              'can_auto_log_rpm': 450, 'can_auto_log_stop_delay': 4,
                                              'mass_flow_enable': False, 'no_buttons': True}))

                def check_request():
                    controller = logger.engine_rpm_logging
                    if can_enabled and auto_enabled:
                        self.assertIsInstance(controller, logger.EngineRpmLoggingController)
                        self.assertEqual((controller.threshold_rpm, controller.stop_delay_s), (450, 4))
                    else:
                        self.assertIsNone(controller)
                    logger.stop_requested.set()
                    return False

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch('sys.argv', ['logger', '--config', str(config)]))
                    for name in ('start_dashboard', 'set_log_health', 'update_latest', 'set_latest_warning', 'monitor_idle_can'):
                        stack.enter_context(mock.patch.object(logger, name))
                    stack.enter_context(mock.patch.object(logger.signal, 'signal'))
                    stack.enter_context(mock.patch.object(logger, 'steering_reader', None))
                    stack.enter_context(mock.patch.object(logger, 'mass_flow_reader', None))
                    stack.enter_context(mock.patch.object(logger, 'find_log_directory', return_value=Path(directory)))
                    stack.enter_context(mock.patch.object(logger, 'create_boot_log_directory', return_value=Path(directory)))
                    stack.enter_context(mock.patch.object(logger, 'free_space_bytes', return_value=10**10))
                    stack.enter_context(mock.patch.object(logger, 'wait_for_acquisition_request', side_effect=check_request))
                    logger.main()
        logger.stop_requested.clear()

    def test_idle_monitor_releases_receiver_before_acquisition(self):
        stopped = threading.Event()
        ready = threading.Event()

        def receive(config, started_at, stop, *, recording=None):
            self.assertIsNone(recording)
            ready.set()
            if stop.wait(2):
                stopped.set()

        with mock.patch.object(logger, 'run_can_logger', side_effect=receive):
            with logger.monitor_idle_can({'enabled': True}):
                self.assertTrue(ready.wait(1))
            self.assertTrue(stopped.is_set())


if __name__ == '__main__':
    unittest.main()
