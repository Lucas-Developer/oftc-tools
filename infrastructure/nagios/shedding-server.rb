#!/usr/bin/ruby

require 'net/IRC'
require 'drb/drb'
require 'yaml'
require 'optparse'
require 'monitor'

def show_help(parser, code=0, io=STDOUT)
  program_name = File.basename($0, '.*')
  io.puts "Usage: #{program_name} <configfile>"
  io.puts parser.summarize
  exit(code)
end
ARGV.options do |opts|
        opts.on_tail("-h", "--help" , "Display this help screen")                { show_help(opts) }
        opts.parse!
end
show_help(ARGV.options, 1, STDERR) if ARGV.length != 1
CONFFILE = ARGV.shift

unless File.exists?(CONFFILE)
	STDERR.puts "File #{CONFFILE} does not exist"
	exit 1;
end
CONF = YAML::load( File.open(CONFFILE) )

error = false
%w{operuser operpass nick user gecos server port password bindip usessl timeout}.each do |key|
	unless CONF.has_key?(key)
		STDERR.puts "Key #{key} not found in config file #{CONFFILE}."
		error = true
	end
end
exit 1 if error

OPER_USER = CONF['operuser']
OPER_PASS = CONF['operpass']
NICK = CONF['nick']
USER = CONF['user']
GCOS = CONF['gecos']
SERVER = CONF['server'].untaint
PORT = CONF['port']
PASSWORD = CONF['password']
BINDIP = CONF['bindip']
USESSL = CONF['usessl']
TIMEOUT = CONF['timeout']


class SheddingCheck
  def initialize
    @conn = nil
    @is_oper = false
    @in_stats = false # XXX get rid of this variable
    @stats = {}
    @stats.extend(MonitorMixin)

    Thread.new do
      @conn = IRC.new(NICK, USER, GCOS)
      @conn.debug = true
      @conn.add_handler('219', method(:stats_end))
      @conn.add_handler('220', method(:stats_Pline))
      @conn.add_handler('249', method(:stats_Eline))
      @conn.add_handler('381', method(:is_oper))
      @conn.add_handler('402', method(:no_such_server))
      @conn.add_handler('RECONNECT', method(:reconnect))
      @conn.add_handler('CONNECTED', method(:connected))
      @conn.connect(SERVER, PORT, PASSWORD, BINDIP, USESSL)
      @conn.run
    end
  end

  def reconnect(sender, source, params)
    puts '... reconnecting ...'
    @results = ['timeout'] if @in_stats
    @in_stats = false
    @is_oper = false
  end

  def is_oper(sender, source, params)
    @is_oper = true
  end

  def connected(sender, source, params)
    puts "%s is connected" % sender.nickname
    sender.send("OPER %s %s" % [OPER_USER, OPER_PASS])
    @in_stats = false
  end



  def no_such_server(sender, source, params)
    target = params.shift  # that's me
    servername = params.shift
    errormessage = params.shift # "No such server"
    errormessage.chomp!
    cancel_requests(servername, errormessage)
  end

  def stats_Pline(sender, source, params)
    register_line(source, 'P', params)
  end

  def stats_Eline(sender, source, params)
    register_line(source, 'E', params)
  end

  def stats_end(sender, source, params)
    target = params.shift  # that's me
    letter = params.shift
    endofstatsbanner = params.shift

    request_done(source, letter)
  end



  def register_line(servername, letter, line)
    @stats.synchronize do
      if @stats.has_key?(servername) and @stats[servername].has_key?(letter)
	@stats[servername][letter]['data'] << line
      end
    end
  end

  def cancel_requests(servername, reason)
    @stats.synchronize do
      if @stats.has_key?(servername)
	@stats[servername].each_key do |letter|
	  @stats[servername][letter]['error'] = reason
	  @stats[servername][letter]['cond'].broadcast
	end
      end
    end
  end

  def request_done(servername, letter)
    @stats.synchronize do
      if @stats.has_key?(servername) and @stats[servername].has_key?(letter)
	@stats[servername][letter]['cond'].broadcast
      end
    end
  end


  def get_stats(servername, letter)
    return false, 'not opered' unless @is_oper
    throw 'only support P and E' unless letter == "E" or letter == "P"
    @stats.synchronize do
      @stats[servername] = {} unless @stats[servername]
      @stats[servername][letter] = {} unless @stats[servername][letter]
      @stats[servername][letter]['cond'] = @stats.new_cond unless @stats[servername][letter]['cond']
      @stats[servername][letter]['refcounter'] = 0 unless @stats[servername][letter]['refcounter']

      if @stats[servername][letter]['refcounter'] == 0
	# so, this still fails in an ugly way when
	# 1) a prior request times out, and we return
	# 2) then we receive a few stat lines,
	# 3) then we make a new request, creating a new data array
	# 4) we receive the rest of the stat lines for the previous request and its end-of-stats
	# 
	# I don't think that's horribly likely to happen, but it would be nice if it could
	# be solved anyway.  Not sure how to do it in a clean way however
	@stats[servername][letter].delete('error')
	@stats[servername][letter].delete('data')

	@stats[servername][letter]['data'] = []
	@conn.send("STATS %s %s" % [letter, servername])
      end

      @stats[servername][letter]['refcounter'] += 1
      res = @stats[servername][letter]['cond'].wait(TIMEOUT)
      @stats[servername][letter]['refcounter'] -= 1

      if res
        if @stats[servername][letter].has_key?('error')
	  return false, @stats[servername][letter]['error']
	else
	  return true, @stats[servername][letter]['data']
	end
      else
	return false, 'timeout'
      end
    end
  end

  def quit
    @conn.quit('')
  end
end

Thread.abort_on_exception = true

inthandler = proc{
  puts "^C pressed"
  $foo.quit
  DRb.stop_service
}

trap("SIGINT", inthandler)
$SAFE = 1
URI = "druby://localhost:8787"
$foo = SheddingCheck.new
DRb.start_service(URI, $foo)

DRb.thread.join