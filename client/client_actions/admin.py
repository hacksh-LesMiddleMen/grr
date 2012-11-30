#!/usr/bin/env python
# Copyright 2012 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Client actions related to administrating the client and its configuration."""


import os
import platform
import socket
import time


import psutil

import logging

from grr.client import actions
from grr.client import client_config
from grr.client import conf
from grr.lib import stats
from grr.proto import jobs_pb2


class Echo(actions.ActionPlugin):
  """Returns a message to the server."""
  in_protobuf = jobs_pb2.PrintStr
  out_protobuf = jobs_pb2.PrintStr

  def Run(self, args):
    self.SendReply(args)


class GetHostname(actions.ActionPlugin):
  """Retrieves the host name of the client."""
  out_protobuf = jobs_pb2.DataBlob

  def Run(self, unused_args):
    self.SendReply(string=socket.gethostname())


class GetPlatformInfo(actions.ActionPlugin):
  """Retrieves platform information."""
  out_protobuf = jobs_pb2.Uname

  def Run(self, unused_args):
    uname = platform.uname()
    self.SendReply(system=uname[0],
                   node=uname[1],
                   release=uname[2],
                   version=uname[3],
                   machine=uname[4])


class Kill(actions.ActionPlugin):
  """A client action for terminating (killing) the client.

  Used for testing process respawn.
  """
  out_protobuf = jobs_pb2.GrrMessage

  def Run(self, unused_arg):
    """Run the kill."""
    # Send a message back to the service to say that we are about to shutdown.
    reply = jobs_pb2.GrrStatus()
    reply.status = jobs_pb2.GrrStatus.OK
    # Queue up the response message, jump the queue.
    self.SendReply(reply, message_type=jobs_pb2.GrrMessage.STATUS,
                   priority=jobs_pb2.GrrMessage.HIGH_PRIORITY + 1)

    # Give the http thread some time to send the reply.
    self.grr_worker.Sleep(10)

    # Die ourselves.
    logging.info("Dying on request.")
    os._exit(242)  # pylint: disable=W0212


class Hang(actions.ActionPlugin):
  """A client action for simulating the client becoming unresponsive (hanging).

  Used for testing nanny terminating the client.
  """
  in_protobuf = jobs_pb2.DataBlob

  def Run(self, arg):
    # Sleep a really long time.
    time.sleep(arg.integer or 6000)


class BusyHang(actions.ActionPlugin):
  """A client action that burns cpu cycles. Used for testing cpu limits."""
  in_protobuf = jobs_pb2.DataBlob

  def Run(self, arg):
    end = time.time() + (arg.integer or 5)
    while time.time() < end:
      pass


class Bloat(actions.ActionPlugin):
  """A client action that uses lots of memory for testing."""
  in_protobuf = jobs_pb2.DataBlob

  def Run(self, arg):

    iterations = arg.integer or 1024  # Gives 1 gb.

    l = []

    for _ in range(iterations):
      l.append("X" * 1048576)  # 1 mb.

    time.sleep(60)


class GetConfig(actions.ActionPlugin):
  """Retrieves the running configuration parameters."""
  in_protobuf = None
  out_protobuf = jobs_pb2.GRRConfig

  def Run(self, unused_arg):
    out = jobs_pb2.GRRConfig()
    for field in out.DESCRIPTOR.fields_by_name:
      if hasattr(conf.FLAGS, field):
        setattr(out, field, getattr(conf.FLAGS, field))
    self.SendReply(out)


class UpdateConfig(actions.ActionPlugin):
  """Updates configuration parameters on the client."""
  in_protobuf = jobs_pb2.GRRConfig

  UPDATEABLE_FIELDS = ["compression",
                       "foreman_check_frequency",
                       "location",
                       "max_post_size",
                       "max_out_queue",
                       "poll_min",
                       "poll_max",
                       "poll_slew",
                       "rss_max",
                       "verbose"]

  def Run(self, arg):
    """Does the actual work."""
    updated_keys = []
    disallowed_fields = []
    for field, value in arg.ListFields():
      if field.name in self.UPDATEABLE_FIELDS:
        setattr(conf.FLAGS, field.name, value)
        updated_keys.append(field.name)
      else:
        disallowed_fields.append(field.name)

    if disallowed_fields:
      logging.warning("Received an update request for restricted field(s) %s.",
                      ",".join(disallowed_fields))
    try:
      conf.PARSER.UpdateConfig(updated_keys)
    except (IOError, OSError):
      pass


class GetClientInfo(actions.ActionPlugin):
  """Obtains information about the GRR client installed."""
  out_protobuf = jobs_pb2.ClientInformation

  def Run(self, unused_args):

    self.SendReply(
        client_name=client_config.GRR_CLIENT_NAME,
        client_version=client_config.GRR_CLIENT_VERSION,
        build_time=client_config.GRR_CLIENT_BUILDTIME,
        )


class GetClientStats(actions.ActionPlugin):
  """This retrieves some stats about the GRR process."""
  in_protobuf = None
  out_protobuf = jobs_pb2.ClientStats

  def Run(self, unused_arg):
    """Returns the client stats."""
    response = jobs_pb2.ClientStats()
    proc = psutil.Process(os.getpid())
    response.RSS_size, response.VMS_size = proc.get_memory_info()
    response.memory_percent = proc.get_memory_percent()
    response.bytes_received = stats.STATS.Get("grr_client_received_bytes")
    response.bytes_sent = stats.STATS.Get("grr_client_sent_bytes")
    response.create_time = long(proc.create_time * 1e6)
    response.boot_time = long(psutil.BOOT_TIME * 1e6)

    samples = self.grr_worker.stats_collector.cpu_samples
    for (timestamp, user, system, percent) in samples:
      sample = response.cpu_samples.add()
      sample.timestamp = long(timestamp * 1e6)
      sample.user_cpu_time = user
      sample.system_cpu_time = system
      sample.cpu_percent = percent

    samples = self.grr_worker.stats_collector.io_samples
    for (timestamp, read_bytes, write_bytes) in samples:
      sample = response.io_samples.add()
      sample.timestamp = long(timestamp * 1e6)
      sample.read_bytes = read_bytes
      sample.write_bytes = write_bytes

    self.Send(response)

  def Send(self, response):
    self.SendReply(response)


class GetClientStatsAuto(GetClientStats):
  """This class is used to send the reply to a well known flow on the server."""

  def Send(self, response):
    self.grr_worker.SendReply(response,
                              session_id="W:Stats",
                              response_id=0,
                              request_id=0,
                              priority=jobs_pb2.GrrMessage.LOW_PRIORITY,
                              message_type=jobs_pb2.GrrMessage.MESSAGE,
                              require_fastpoll=False)


class SendStartupInfo(actions.ActionPlugin):

  in_protobuf = None
  out_protobuf = jobs_pb2.StartupInfo

  well_known_session_id = "W:Startup"

  def Run(self, unused_arg, ttl=None):
    """Returns the startup information."""

    response = jobs_pb2.StartupInfo()
    client_info = jobs_pb2.ClientInformation(
        client_name=client_config.GRR_CLIENT_NAME,
        client_version=client_config.GRR_CLIENT_VERSION,
        build_time=client_config.GRR_CLIENT_BUILDTIME)

    response.client_info.MergeFrom(client_info)
    response.boot_time = long(psutil.BOOT_TIME * 1e6)

    self.grr_worker.SendReply(response,
                              session_id=self.well_known_session_id,
                              response_id=0,
                              request_id=0,
                              priority=jobs_pb2.GrrMessage.LOW_PRIORITY,
                              message_type=jobs_pb2.GrrMessage.MESSAGE,
                              require_fastpoll=False,
                              ttl=ttl)