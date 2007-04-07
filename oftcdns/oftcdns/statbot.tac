# Copyright (C) 2007 Luca Filipozzi

from twisted.application import internet, service
from twisted.internet import protocol, ssl, task
from twisted.python import log
from twisted.spread import pb
from twisted.words.protocols import irc
from twistedsnmp import agent, agentprotocol, bisectoidstore
from twistedsnmp.pysnmpproto import oid
import logging, os, string, syck, time

irc.RPL_STATSPORTINFO = '220'
irc.symbolic_to_numeric['RPL_STATSPORTINFO'] = '220'
irc.numeric_to_symbolic['220'] = 'RPL_STATSPORTINFO'

class Node:
  """ keep track of a node's statistics """
  def __init__(self, name):
    """ class constructor """
    self.name = name
    self.active = True
    self.rank = 10000
    self.last = time.time()
  def check(self, period):
    """ check status """
    if self.last + 2 * period < time.time():
      self.active = False
      self.rank = 10000
  def __str__(self):
    """ string representation """
    return "%s%s(%s)" % (self.name, {True: '+', False: '-'}[self.active], self.rank)

class MyBot(irc.IRCClient):
  """ subclass of IRCClient that implements our bot's functionality """
  def __init__(self, config):
    """ class constructor """
    self.__dict__.update({'opername': None, 'operpass': None})
    self.__dict__.update(config)
    self.queryTimer = task.LoopingCall(self.irc_QRY_STATSPORTINFO)
    self.aliveTimer = task.LoopingCall(self.ping)
    self.last = time.time()
  def __del__(self):
    """ class destructor """
    if self.queryTimer.running:
      self.queryTimer.stop()
    del self.queryTimer
    if self.aliveTimer.running:
      self.aliveTimer.stop()
    del self.aliveTimer
  def connectionMade(self):
    """ clear the dictionary of nodes when connection has been made """
    self.factory.nodes = {}
    irc.IRCClient.connectionMade(self)
  def connectionLost(self, reason):
    """ stop the timer when connection has been lost """
    if self.queryTimer.running:
      self.queryTimer.stop()
    if self.aliveTimer.running:
      self.aliveTimer.stop()
    irc.IRCClient.connectionLost(self, reason)
  def signedOn(self):
    """ once signed on, oper up if configured to do so else join channel """
    if not self.aliveTimer.running:
      self.aliveTimer.start(self.period)
    if self.opername and self.operpass:
      self.sendLine("OPER %s %s" % (self.opername, self.operpass))
    else:
      self.join(self.channel)
  def irc_RPL_YOUREOPER(self, prefix, params):
    """ oper up succeeded, so join channel """
    self.join(self.channel)
  def irc_ERR_PASSWDMISMATCH(self, prefix, params):
    """ oper up failed due to password mismatch; ignore and join channel """
    self.join(self.channel)
  def irc_ERR_NOOPERHOST(self, prefix, params):
    """ oper up failed due to no operhost line; ignore and join channel """
    self.join(self.channel)
  def irc_ERR_NEEDMOREPARAMS(self, prefix, params):
    """ oper up failed due to incorrect command syntax; ignore and join channel """
    self.join(self.channel)
  def joined(self, channel):
    """ once channel joined, send LINKS query to server """
    self.sendLine("LINKS")
  def kickedFrom(self, channel, kicker, message):
    """ rejoin channel if kicked """
    self.join(channel)
  def irc_RPL_LINKS(self, prefix, params):
    """ recv LINKS reply from server; add to dict of nodes """
    for node in params[1:2]:
      if node not in self.factory.nodes:
        self.factory.nodes[node] = Node(node)
  def irc_RPL_ENDOFLINKS(self, prefix, params):
    """ recv END OF LINKS reply from server; start gathering statistics """
    if not self.queryTimer.running:
      self.queryTimer.start(self.period)
  def irc_QRY_STATSPORTINFO(self):
    """ send STATS P query to nodes """
    for node in self.factory.nodes.itervalues():
      node.check(self.period)
      node.rank_tmp = 0
      node.active_tmp = False
      self.sendLine("STATS P %s" % node.name)
  def irc_RPL_STATSPORTINFO(self, prefix, params):
    """ recv STATS P reply from nodes """
    node = self.factory.nodes[prefix]
    if params[2] == '6667':
      node.active_tmp = {'active': True, 'disabled': False}[params[-1]]
    node.rank_tmp += string.atoi(params[4])
  def irc_RPL_ENDOFSTATS(self, prefix, params):
    """ recv END OF STATS reply from nodes """
    node = self.factory.nodes[prefix]
    node.active = node.active_tmp
    node.rank = node.rank_tmp
    node.last = time.time()
  def ping(self):
    """ send keepalive ping """
    if self.last + 4 * self.period < time.time():
      self.transport.loseConnection()
    else:
      self.sendLine("PING %s" % self.nickname)
  def irc_PONG(self, prefix, params):
    """ recv keepalive pong """
    self.last = time.time()
  def privmsg(self, username, channel, msg):
    """ command dispatcher """
    username = username.split('!', 1)[0]
    if channel == self.nickname:
      self.msg(username, "privmsgs not accepted")
    elif msg.startswith(self.nickname + ": "):
      method = 'do_' + msg[len(self.nickname)+2:].split(' ')[0]
      if hasattr(self, method):
        getattr(self, method)(username, channel)
  def do_reload(self, username, channel):
    """ handle reload request """
    if self.queryTimer.running:
      self.queryTimer.stop()
    self.factory.nodes = {}
    self.sendLine("LINKS")
    self.msg(channel, "%s: reloading" % username)
  def do_status(self, username, channel):
    """ handle status request """
    nodes = self.factory.nodes.keys()
    nodes.sort()
    self.msg(channel, "%s: node status" % username)
    self.msg(channel, " ".join([self.factory.nodes[node].__str__() for node in nodes]), 120)

class MyBotFactory(protocol.ReconnectingClientFactory):
  """ subclass of ReconnectingClientFactory that knows how to instantiate MyBot objects and keeps track of Nodes """
  def __init__(self, config):
    """ class constructor """
    self.config = config
    self.protocol = MyBot
    self.MaxDelay = 30
    self.nodes = {}

  def buildProtocol(self, addr):
    """ instantiate a MyBot object """
    p = self.protocol(self.config)
    p.factory = self
    return p

  def stats(self):
    """ return array of tuples of node statistics """
    return [(x.name, x.active, x.rank) for x in self.nodes.itervalues()]

class MyPBServer(pb.Root):
  """ subclass of pb.Root that implements the method(s) available to the remote client """
  def __init__(self, ircFactory):
    """ class constructor """
    self.ircFactory = ircFactory

  def remote_stats(self):
    """ reply to remote with stats """
    return self.ircFactory.stats()

def Application():
  """ the application """
  logging.basicConfig(level=logging.WARNING, format='%(message)s')

  f = open(os.environ['configfile'])
  config = syck.load(f.read())
  f.close()
  application = service.Application('statbot')
  serviceCollection = service.IServiceCollection(application)

  # irc client
  ircFactory = MyBotFactory(config['irc']['bot'])
  if config['irc']['ssl'] == True:
    internet.SSLClient(config['irc']['server'], config['irc']['port'], ircFactory, ssl.ClientContextFactory()).setServiceParent(serviceCollection)
  else:
    internet.TCPClient(config['irc']['server'], config['irc']['port'], ircFactory).setServiceParent(serviceCollection)

  # pb server
  pbFactory = pb.PBServerFactory(MyPBServer(ircFactory))
  internet.TCPServer(config['pb']['port'], pbFactory, interface=config['pb']['interface']).setServiceParent(serviceCollection)

  # snmp server
  subconfig = config['snmp']
  oids = []
  oids.append(('.1.3.6.1.2.1.1.1.0', subconfig['description'])) # system.sysDescr
  oids.append(('.1.3.6.1.2.1.1.4.0', subconfig['contact']))     # system.sysContact
  oids.append(('.1.3.6.1.2.1.1.5.0', subconfig['name']))        # system.sysName
  oids.append(('.1.3.6.1.2.1.1.6.0', subconfig['location']))    # system.sysLocation
  snmpAgent = agent.Agent(bisectoidstore.BisectOIDStore([(oid.OID(k),v) for k,v in oids]))
  internet.UDPServer(subconfig['port'], agentprotocol.AgentProtocol(agent = snmpAgent), interface=subconfig['interface']).setServiceParent(serviceCollection)

  return application

application = Application()

# vim: set ft=python ts=2 sw=2 et fdm=indent:
