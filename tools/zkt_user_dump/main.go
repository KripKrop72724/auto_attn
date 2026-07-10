package main

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

const (
	defaultCommKey            = 12345
	defaultZKPort             = 4370
	defaultTelnetPort         = 23
	defaultTelnetUsername     = "root"
	defaultTelnetPassword     = ""
	defaultTelnetExpectBanner = "Linux"
	exportVersion             = "2026-07-02.3"
)

const (
	ushrtMax = 65535

	cmdOptionsRRQ  = 11
	cmdGetFreeSize = 50
	cmdConnect     = 1000
	cmdExit        = 1001
	cmdAuth        = 1102
	cmdData        = 1501
	cmdFreeData    = 1502
	cmdAckOK       = 2000
	cmdPrepareData = 1500
	cmdAckUnauth   = 2005

	cmdReadWithBuffer = 1503
	cmdReadChunk      = 1504
	cmdUserTempRRQ    = 9
	fctUser           = 5

	machinePrepareData1 = 20560
	machinePrepareData2 = 32130
)

type config struct {
	CommKey            int
	Port               int
	TelnetPort         int
	Timeout            time.Duration
	ScanTimeout        time.Duration
	Workers            int
	MaxHostsPerSubnet  int
	OutDir             string
	IPs                []string
	Subnets            []string
	TelnetEnabled      bool
	TelnetUsername     string
	TelnetPassword     string
	TelnetExpectBanner string
	CommKeyProvided    bool
	Verbose            bool
	Pause              bool
	NoPause            bool
}

type candidate struct {
	IP        string `json:"ip"`
	Port      int    `json:"port"`
	Subnet    string `json:"subnet,omitempty"`
	Interface string `json:"interface,omitempty"`
}

type userRecord struct {
	UID       string            `json:"uid"`
	UserID    string            `json:"user_id"`
	Name      string            `json:"name,omitempty"`
	Privilege string            `json:"privilege,omitempty"`
	Password  string            `json:"password,omitempty"`
	GroupID   string            `json:"group_id,omitempty"`
	Card      string            `json:"card,omitempty"`
	Source    string            `json:"source,omitempty"`
	Raw       map[string]string `json:"raw,omitempty"`
}

type deviceInfo struct {
	Serial     string `json:"serial,omitempty"`
	Platform   string `json:"platform,omitempty"`
	DeviceName string `json:"device_name,omitempty"`
	Protocol   string `json:"protocol,omitempty"`
}

type deviceResult struct {
	IP            string       `json:"ip"`
	Port          int          `json:"port"`
	Method        string       `json:"method"`
	Info          deviceInfo   `json:"info,omitempty"`
	Users         []userRecord `json:"users"`
	Error         string       `json:"error,omitempty"`
	TelnetLogPath string       `json:"telnet_log_path,omitempty"`
}

type exportReport struct {
	GeneratedAt string         `json:"generated_at"`
	Version     string         `json:"version"`
	CommKey     int            `json:"comm_key_tried"`
	Devices     []deviceResult `json:"devices"`
	TotalUsers  int            `json:"total_users"`
}

func main() {
	code := run()
	os.Exit(code)
}

func run() int {
	cfg := parseFlags()
	defer pauseIfNeeded(cfg)

	if !cfg.CommKeyProvided && stdinIsTerminal() {
		cfg.CommKey = promptCommKey(cfg.CommKey)
	}

	if err := os.MkdirAll(cfg.OutDir, 0o755); err != nil {
		fmt.Printf("Could not create output directory %s: %v\n", cfg.OutDir, err)
		return 1
	}

	fmt.Println("ZKT user export")
	fmt.Printf("Target: Windows 11 64-bit build, local run version %s\n", exportVersion)
	fmt.Printf("SDK comm key: %d\n", cfg.CommKey)

	candidates := cfg.manualCandidates()
	if len(candidates) == 0 {
		fmt.Printf("Scanning local connected subnets for TCP port %d...\n", cfg.Port)
		scanned, err := scanForCandidates(cfg)
		if err != nil {
			fmt.Printf("Scan failed: %v\n", err)
			return 1
		}
		candidates = scanned
	}

	if len(candidates) == 0 {
		fmt.Println("No ZKT candidates were found.")
		fmt.Println("Try --ip <exact device IP> if you know it, or --subnet <CIDR> such as --subnet 172.10.10.0/24.")
		return 2
	}

	fmt.Printf("Found %d candidate(s).\n", len(candidates))
	for _, item := range candidates {
		fmt.Printf("  - %s:%d", item.IP, item.Port)
		if item.Subnet != "" {
			fmt.Printf(" on %s", item.Subnet)
		}
		fmt.Println()
	}

	report := exportReport{
		GeneratedAt: time.Now().Format(time.RFC3339),
		Version:     exportVersion,
		CommKey:     cfg.CommKey,
	}

	for _, cand := range candidates {
		result := deviceResult{IP: cand.IP, Port: cand.Port}
		fmt.Printf("\nTrying %s:%d via ZKT SDK protocol...\n", cand.IP, cand.Port)
		users, info, method, err := fetchUsersViaSDK(cand.IP, cand.Port, cfg.CommKey, cfg.Timeout, cfg.Verbose)
		if err == nil {
			result.Method = method
			result.Info = info
			result.Users = users
			report.TotalUsers += len(users)
			fmt.Printf("  OK: loaded %d user(s) with comm key %d.\n", len(users), cfg.CommKey)
			report.Devices = append(report.Devices, result)
			continue
		}

		fmt.Printf("  SDK failed: %v\n", err)
		result.Error = err.Error()
		if cfg.TelnetEnabled {
			fmt.Printf("  Trying telnet fallback on %s:%d...\n", cand.IP, cfg.TelnetPort)
			telnetUsers, telnetLog, telnetErr := fetchUsersViaTelnet(cand.IP, cfg)
			if telnetLog != "" {
				path, writeErr := writeTelnetLog(cfg.OutDir, cand.IP, telnetLog)
				if writeErr == nil {
					result.TelnetLogPath = path
				}
			}
			if telnetErr == nil {
				result.Method = "telnet"
				result.Users = telnetUsers
				result.Error = ""
				report.TotalUsers += len(telnetUsers)
				fmt.Printf("  OK: telnet fallback loaded %d user(s).\n", len(telnetUsers))
			} else {
				result.Error = fmt.Sprintf("sdk failed: %v; telnet failed: %v", err, telnetErr)
				fmt.Printf("  Telnet failed: %v\n", telnetErr)
			}
		}
		report.Devices = append(report.Devices, result)
	}

	jsonPath, err := writeJSONReport(cfg.OutDir, report)
	if err != nil {
		fmt.Printf("Could not write JSON report: %v\n", err)
		return 1
	}
	csvPath, err := writeCSVReport(cfg.OutDir, report)
	if err != nil {
		fmt.Printf("Could not write CSV report: %v\n", err)
		return 1
	}

	fmt.Printf("\nWrote JSON: %s\n", jsonPath)
	fmt.Printf("Wrote CSV:  %s\n", csvPath)
	fmt.Printf("Total users exported: %d\n", report.TotalUsers)
	if report.TotalUsers == 0 {
		return 3
	}
	return 0
}

func parseFlags() config {
	var cfg config
	var ipList string
	var subnetList string
	var timeoutSeconds float64
	var scanTimeoutSeconds float64

	defaultRuntimeCommKey := envIntDefault("ZKT_COMM_KEY", defaultCommKey)
	flag.IntVar(&cfg.CommKey, "comm-key", defaultRuntimeCommKey, "ZKT comm key to try first")
	flag.IntVar(&cfg.CommKey, "comkey", defaultRuntimeCommKey, "alias for --comm-key")
	flag.IntVar(&cfg.CommKey, "commkey", defaultRuntimeCommKey, "alias for --comm-key")
	flag.IntVar(&cfg.CommKey, "key", defaultRuntimeCommKey, "alias for --comm-key")
	flag.IntVar(&cfg.Port, "port", defaultZKPort, "ZKT SDK TCP/UDP port")
	flag.IntVar(&cfg.TelnetPort, "telnet-port", defaultTelnetPort, "telnet port")
	flag.Float64Var(&timeoutSeconds, "timeout", 7, "SDK/telnet operation timeout in seconds")
	flag.Float64Var(&scanTimeoutSeconds, "scan-timeout", 0.45, "TCP scan timeout per host in seconds")
	flag.IntVar(&cfg.Workers, "workers", 128, "scan worker count")
	flag.IntVar(&cfg.MaxHostsPerSubnet, "max-hosts", 254, "maximum hosts to scan per subnet")
	flag.StringVar(&ipList, "ip", "", "comma-separated device IP(s); skips subnet discovery")
	flag.StringVar(&subnetList, "subnet", "", "comma-separated CIDR subnet(s), e.g. 192.168.1.0/24")
	flag.StringVar(&cfg.OutDir, "out", ".", "directory for CSV/JSON output")
	flag.BoolVar(&cfg.Verbose, "verbose", false, "print extra diagnostics")
	flag.BoolVar(&cfg.TelnetEnabled, "telnet", true, "try telnet fallback after SDK failure")
	flag.StringVar(&cfg.TelnetUsername, "telnet-user", envDefault("TELNET_USERNAME", defaultTelnetUsername), "telnet username")
	flag.StringVar(&cfg.TelnetPassword, "telnet-pass", envDefault("TELNET_PASSWORD", defaultTelnetPassword), "telnet password")
	flag.StringVar(&cfg.TelnetExpectBanner, "telnet-banner", envDefault("ZKT_TELNET_EXPECT_BANNER", defaultTelnetExpectBanner), "text expected from telnet banner or uname")
	flag.BoolVar(&cfg.Pause, "pause", false, "wait for Enter before exiting")
	flag.BoolVar(&cfg.NoPause, "no-pause", false, "do not wait for Enter before exiting")
	_ = flag.CommandLine.Parse(normalizeWindowsArgs(os.Args[1:]))
	flag.Visit(func(item *flag.Flag) {
		if item.Name == "comm-key" || item.Name == "comkey" || item.Name == "commkey" || item.Name == "key" {
			cfg.CommKeyProvided = true
		}
	})

	cfg.Timeout = secondsDuration(timeoutSeconds)
	cfg.ScanTimeout = secondsDuration(scanTimeoutSeconds)
	cfg.IPs = splitCSV(ipList)
	cfg.Subnets = splitCSV(subnetList)
	if cfg.Workers <= 0 {
		cfg.Workers = 1
	}
	if cfg.MaxHostsPerSubnet <= 0 {
		cfg.MaxHostsPerSubnet = 254
	}
	if cfg.OutDir == "" {
		cfg.OutDir = "."
	}
	return cfg
}

func envDefault(name string, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envIntDefault(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func normalizeWindowsArgs(args []string) []string {
	normalized := make([]string, 0, len(args))
	for _, arg := range args {
		if strings.HasPrefix(arg, "/") && len(arg) > 1 && !strings.HasPrefix(arg, "//") {
			arg = "--" + strings.TrimPrefix(arg, "/")
			if index := strings.Index(arg, ":"); index >= 0 {
				arg = arg[:index] + "=" + arg[index+1:]
			}
		}
		normalized = append(normalized, arg)
	}
	return normalized
}

func promptCommKey(defaultKey int) int {
	fmt.Printf("Enter ZKT comm key [%d]: ", defaultKey)
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil {
		return defaultKey
	}
	line = strings.TrimSpace(line)
	if line == "" {
		return defaultKey
	}
	value, err := strconv.Atoi(line)
	if err != nil || value < 0 {
		fmt.Printf("Invalid comm key %q; using %d.\n", line, defaultKey)
		return defaultKey
	}
	return value
}

func secondsDuration(value float64) time.Duration {
	if value <= 0 {
		value = 1
	}
	return time.Duration(value * float64(time.Second))
}

func splitCSV(value string) []string {
	parts := strings.Split(value, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func (cfg config) manualCandidates() []candidate {
	var out []candidate
	for _, ip := range cfg.IPs {
		if parsed := net.ParseIP(ip); parsed != nil && parsed.To4() != nil {
			out = append(out, candidate{IP: parsed.String(), Port: cfg.Port})
		} else {
			fmt.Printf("Ignoring invalid --ip value: %s\n", ip)
		}
	}
	if len(out) > 0 {
		return out
	}
	return nil
}

func pauseIfNeeded(cfg config) {
	if cfg.NoPause {
		return
	}
	if cfg.Pause || stdinIsTerminal() {
		fmt.Print("\nPress Enter to exit...")
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	}
}

func stdinIsTerminal() bool {
	info, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return (info.Mode() & os.ModeCharDevice) != 0
}

func scanForCandidates(cfg config) ([]candidate, error) {
	subnets, err := discoverSubnets(cfg)
	if err != nil {
		return nil, err
	}
	if len(subnets) == 0 {
		return nil, nil
	}

	targets := map[string]candidate{}
	for _, subnet := range subnets {
		for _, host := range hostsForSubnet(subnet.network, cfg.MaxHostsPerSubnet) {
			if _, exists := targets[host]; !exists {
				targets[host] = candidate{
					IP:        host,
					Port:      cfg.Port,
					Subnet:    subnet.network.String(),
					Interface: subnet.interfaceName,
				}
			}
		}
	}
	if len(targets) == 0 {
		return nil, nil
	}

	jobs := make(chan candidate)
	results := make(chan candidate)
	var wg sync.WaitGroup
	workers := cfg.Workers
	if workers > len(targets) {
		workers = len(targets)
	}
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for cand := range jobs {
				if tcpPortOpen(cand.IP, cand.Port, cfg.ScanTimeout) {
					results <- cand
				}
			}
		}()
	}

	go func() {
		for _, cand := range targets {
			jobs <- cand
		}
		close(jobs)
		wg.Wait()
		close(results)
	}()

	var found []candidate
	for cand := range results {
		found = append(found, cand)
	}
	sort.Slice(found, func(i, j int) bool {
		return ipLess(found[i].IP, found[j].IP)
	})
	return found, nil
}

type discoveredSubnet struct {
	network       *net.IPNet
	interfaceName string
	address       string
}

func discoverSubnets(cfg config) ([]discoveredSubnet, error) {
	if len(cfg.Subnets) > 0 {
		var manual []discoveredSubnet
		for _, item := range cfg.Subnets {
			ip, network, err := net.ParseCIDR(item)
			if err != nil {
				return nil, fmt.Errorf("invalid subnet %q: %w", item, err)
			}
			network.IP = ip.Mask(network.Mask)
			manual = append(manual, discoveredSubnet{network: network, interfaceName: "manual"})
		}
		return manual, nil
	}

	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	seen := map[string]discoveredSubnet{}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		if excludedInterface(iface.Name) {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ip, network := ipAndNetwork(addr)
			if ip == nil || network == nil || ip.To4() == nil {
				continue
			}
			ip = ip.To4()
			if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsMulticast() || ip.IsUnspecified() {
				continue
			}
			network.IP = ip.Mask(network.Mask)
			network = capNetworkToHostSubnet(network, ip, cfg.MaxHostsPerSubnet)
			key := iface.Name + "|" + network.String()
			seen[key] = discoveredSubnet{
				network:       network,
				interfaceName: iface.Name,
				address:       ip.String(),
			}
		}
	}
	out := make([]discoveredSubnet, 0, len(seen))
	for _, subnet := range seen {
		out = append(out, subnet)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].network.String() == out[j].network.String() {
			return out[i].interfaceName < out[j].interfaceName
		}
		return out[i].network.String() < out[j].network.String()
	})
	return out, nil
}

func excludedInterface(name string) bool {
	lower := strings.ToLower(name)
	for _, keyword := range []string{"docker", "vbox", "virtualbox", "vmware", "hyper-v", "hyperv", "tailscale", "zerotier", "utun", "llw", "awdl"} {
		if strings.Contains(lower, keyword) {
			return true
		}
	}
	return false
}

func ipAndNetwork(addr net.Addr) (net.IP, *net.IPNet) {
	switch value := addr.(type) {
	case *net.IPNet:
		return value.IP, &net.IPNet{IP: append(net.IP(nil), value.IP...), Mask: append(net.IPMask(nil), value.Mask...)}
	case *net.IPAddr:
		if value.IP.To4() == nil {
			return nil, nil
		}
		return value.IP, &net.IPNet{IP: value.IP.Mask(net.CIDRMask(24, 32)), Mask: net.CIDRMask(24, 32)}
	default:
		return nil, nil
	}
}

func capNetworkToHostSubnet(network *net.IPNet, ip net.IP, maxHosts int) *net.IPNet {
	ones, bits := network.Mask.Size()
	if bits != 32 {
		return network
	}
	usable := (1 << uint(bits-ones)) - 2
	if usable <= maxHosts {
		return network
	}
	mask := net.CIDRMask(24, 32)
	return &net.IPNet{IP: ip.Mask(mask), Mask: mask}
}

func hostsForSubnet(network *net.IPNet, maxHosts int) []string {
	ip := network.IP.To4()
	if ip == nil {
		return nil
	}
	first := binary.BigEndian.Uint32(ip)
	mask := binary.BigEndian.Uint32(net.IP(network.Mask).To4())
	networkStart := first & mask
	broadcast := networkStart | ^mask

	var hosts []string
	start := networkStart + 1
	end := broadcast
	if broadcast == networkStart {
		start = networkStart
		end = networkStart + 1
	}
	for current := start; current < end && len(hosts) < maxHosts; current++ {
		var raw [4]byte
		binary.BigEndian.PutUint32(raw[:], current)
		hosts = append(hosts, net.IP(raw[:]).String())
	}
	return hosts
}

func tcpPortOpen(ip string, port int, timeout time.Duration) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(ip, strconv.Itoa(port)), timeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

func ipLess(a, b string) bool {
	ipa := net.ParseIP(a).To4()
	ipb := net.ParseIP(b).To4()
	if ipa == nil || ipb == nil {
		return a < b
	}
	return binary.BigEndian.Uint32(ipa) < binary.BigEndian.Uint32(ipb)
}

func fetchUsersViaSDK(ip string, port int, commKey int, timeout time.Duration, verbose bool) ([]userRecord, deviceInfo, string, error) {
	var errs []error
	for _, network := range []string{"tcp", "udp"} {
		client := newZKClient(ip, port, commKey, timeout, network == "tcp", verbose)
		if err := client.connect(); err != nil {
			errs = append(errs, fmt.Errorf("%s connect: %w", network, err))
			continue
		}
		info := client.safeInfo()
		info.Protocol = network
		users, err := client.getUsers()
		_ = client.disconnect()
		if err != nil {
			errs = append(errs, fmt.Errorf("%s get users: %w", network, err))
			continue
		}
		for i := range users {
			users[i].Source = "sdk-" + network
		}
		return users, info, "sdk-" + network, nil
	}
	return nil, deviceInfo{}, "", errors.Join(errs...)
}

type zkClient struct {
	ip             string
	port           int
	commKey        int
	timeout        time.Duration
	tcp            bool
	verbose        bool
	conn           net.Conn
	sessionID      uint16
	replyID        uint16
	response       uint16
	data           []byte
	userPacketSize int
	users          int
}

func newZKClient(ip string, port int, commKey int, timeout time.Duration, tcp bool, verbose bool) *zkClient {
	packetSize := 28
	if tcp {
		packetSize = 72
	}
	return &zkClient{
		ip:             ip,
		port:           port,
		commKey:        commKey,
		timeout:        timeout,
		tcp:            tcp,
		verbose:        verbose,
		replyID:        ushrtMax - 1,
		userPacketSize: packetSize,
	}
}

func (z *zkClient) connect() error {
	network := "udp"
	if z.tcp {
		network = "tcp"
	}
	conn, err := net.DialTimeout(network, net.JoinHostPort(z.ip, strconv.Itoa(z.port)), z.timeout)
	if err != nil {
		return err
	}
	z.conn = conn
	z.sessionID = 0
	z.replyID = ushrtMax - 1
	resp, err := z.sendCommand(cmdConnect, nil, 8)
	if err != nil {
		_ = z.conn.Close()
		return err
	}
	z.sessionID = resp.sessionID
	if resp.code == cmdAckUnauth {
		key := makeCommKey(z.commKey, z.sessionID, 50)
		resp, err = z.sendCommand(cmdAuth, key, 8)
		if err != nil {
			_ = z.conn.Close()
			return err
		}
	}
	if resp.ok {
		return nil
	}
	_ = z.conn.Close()
	if resp.code == cmdAckUnauth {
		return errors.New("unauthenticated")
	}
	return fmt.Errorf("invalid connect response code %d", resp.code)
}

func (z *zkClient) disconnect() error {
	if z.conn == nil {
		return nil
	}
	_, _ = z.sendCommand(cmdExit, nil, 8)
	err := z.conn.Close()
	z.conn = nil
	return err
}

type commandResponse struct {
	ok        bool
	code      uint16
	sessionID uint16
	replyID   uint16
}

func (z *zkClient) sendCommand(command uint16, body []byte, responseSize int) (commandResponse, error) {
	if z.conn == nil {
		return commandResponse{}, errors.New("not connected")
	}
	packet := z.createHeader(command, body)
	if z.tcp {
		frame := make([]byte, 8+len(packet))
		binary.LittleEndian.PutUint16(frame[0:2], machinePrepareData1)
		binary.LittleEndian.PutUint16(frame[2:4], machinePrepareData2)
		binary.LittleEndian.PutUint32(frame[4:8], uint32(len(packet)))
		copy(frame[8:], packet)
		if err := z.writeAll(frame); err != nil {
			return commandResponse{}, err
		}
		inner, err := z.readTCPFrame()
		if err != nil {
			return commandResponse{}, err
		}
		return z.acceptInnerPacket(inner)
	}

	if responseSize < 1024 {
		responseSize = 1024
	}
	if err := z.writeAll(packet); err != nil {
		return commandResponse{}, err
	}
	buf := make([]byte, responseSize+8)
	_ = z.conn.SetReadDeadline(time.Now().Add(z.timeout))
	n, err := z.conn.Read(buf)
	if err != nil {
		return commandResponse{}, err
	}
	return z.acceptInnerPacket(buf[:n])
}

func (z *zkClient) createHeader(command uint16, body []byte) []byte {
	checkBuf := make([]byte, 8+len(body))
	binary.LittleEndian.PutUint16(checkBuf[0:2], command)
	binary.LittleEndian.PutUint16(checkBuf[2:4], 0)
	binary.LittleEndian.PutUint16(checkBuf[4:6], z.sessionID)
	binary.LittleEndian.PutUint16(checkBuf[6:8], z.replyID)
	copy(checkBuf[8:], body)
	checksum := createChecksum(checkBuf)

	sendReply := z.replyID + 1
	if sendReply >= ushrtMax {
		sendReply -= ushrtMax
	}
	out := make([]byte, 8+len(body))
	binary.LittleEndian.PutUint16(out[0:2], command)
	binary.LittleEndian.PutUint16(out[2:4], checksum)
	binary.LittleEndian.PutUint16(out[4:6], z.sessionID)
	binary.LittleEndian.PutUint16(out[6:8], sendReply)
	copy(out[8:], body)
	return out
}

func createChecksum(packet []byte) uint16 {
	var checksum uint32
	i := 0
	remaining := len(packet)
	for remaining > 1 {
		checksum += uint32(binary.LittleEndian.Uint16(packet[i : i+2]))
		if checksum > ushrtMax {
			checksum -= ushrtMax
		}
		i += 2
		remaining -= 2
	}
	if remaining > 0 {
		checksum += uint32(packet[len(packet)-1])
	}
	for checksum > ushrtMax {
		checksum -= ushrtMax
	}
	value := ^int64(checksum)
	for value < 0 {
		value += ushrtMax
	}
	return uint16(value)
}

func makeCommKey(key int, sessionID uint16, ticks byte) []byte {
	var k uint32
	key32 := uint32(key)
	for i := 0; i < 32; i++ {
		k <<= 1
		if key32&(1<<uint(i)) != 0 {
			k |= 1
		}
	}
	k += uint32(sessionID)
	raw := make([]byte, 4)
	binary.LittleEndian.PutUint32(raw, k)
	raw[0] ^= 'Z'
	raw[1] ^= 'K'
	raw[2] ^= 'S'
	raw[3] ^= 'O'
	h0 := binary.LittleEndian.Uint16(raw[0:2])
	h1 := binary.LittleEndian.Uint16(raw[2:4])
	swapped := make([]byte, 4)
	binary.LittleEndian.PutUint16(swapped[0:2], h1)
	binary.LittleEndian.PutUint16(swapped[2:4], h0)
	return []byte{
		swapped[0] ^ ticks,
		swapped[1] ^ ticks,
		ticks,
		swapped[3] ^ ticks,
	}
}

func (z *zkClient) writeAll(buf []byte) error {
	_ = z.conn.SetWriteDeadline(time.Now().Add(z.timeout))
	for len(buf) > 0 {
		n, err := z.conn.Write(buf)
		if err != nil {
			return err
		}
		buf = buf[n:]
	}
	return nil
}

func (z *zkClient) readTCPFrame() ([]byte, error) {
	return z.readTCPFrameWithTimeout(z.timeout)
}

func (z *zkClient) readTCPFrameWithTimeout(timeout time.Duration) ([]byte, error) {
	_ = z.conn.SetReadDeadline(time.Now().Add(timeout))
	header := make([]byte, 8)
	if _, err := io.ReadFull(z.conn, header); err != nil {
		return nil, err
	}
	if binary.LittleEndian.Uint16(header[0:2]) != machinePrepareData1 ||
		binary.LittleEndian.Uint16(header[2:4]) != machinePrepareData2 {
		return nil, errors.New("invalid TCP ZKT frame header")
	}
	length := binary.LittleEndian.Uint32(header[4:8])
	if length < 8 || length > 16*1024*1024 {
		return nil, fmt.Errorf("invalid TCP ZKT frame length %d", length)
	}
	inner := make([]byte, int(length))
	if _, err := io.ReadFull(z.conn, inner); err != nil {
		return nil, err
	}
	return inner, nil
}

func (z *zkClient) acceptInnerPacket(inner []byte) (commandResponse, error) {
	if len(inner) < 8 {
		return commandResponse{}, errors.New("short ZKT response")
	}
	code := binary.LittleEndian.Uint16(inner[0:2])
	sessionID := binary.LittleEndian.Uint16(inner[4:6])
	replyID := binary.LittleEndian.Uint16(inner[6:8])
	z.response = code
	z.sessionID = sessionID
	z.replyID = replyID
	z.data = append(z.data[:0], inner[8:]...)
	return commandResponse{
		ok:        code == cmdAckOK || code == cmdPrepareData || code == cmdData,
		code:      code,
		sessionID: sessionID,
		replyID:   replyID,
	}, nil
}

func (z *zkClient) safeInfo() deviceInfo {
	return deviceInfo{
		Serial:     z.safeOption("~SerialNumber"),
		Platform:   z.safeOption("~Platform"),
		DeviceName: z.safeOption("~DeviceName"),
	}
}

func (z *zkClient) safeOption(name string) string {
	value, err := z.getOption(name)
	if err != nil {
		return ""
	}
	return value
}

func (z *zkClient) getOption(name string) (string, error) {
	resp, err := z.sendCommand(cmdOptionsRRQ, append([]byte(name), 0), 1024)
	if err != nil {
		return "", err
	}
	if !resp.ok {
		return "", fmt.Errorf("option %s response %d", name, resp.code)
	}
	parts := bytes.SplitN(z.data, []byte("="), 2)
	raw := z.data
	if len(parts) == 2 {
		raw = parts[1]
	}
	raw = bytes.SplitN(raw, []byte{0}, 2)[0]
	raw = bytes.ReplaceAll(raw, []byte("="), nil)
	return strings.TrimSpace(validString(raw)), nil
}

func (z *zkClient) readSizes() error {
	resp, err := z.sendCommand(cmdGetFreeSize, nil, 1024)
	if err != nil {
		return err
	}
	if !resp.ok {
		return fmt.Errorf("read sizes response %d", resp.code)
	}
	if len(z.data) < 80 {
		return nil
	}
	fields := make([]int32, 20)
	for i := 0; i < 20; i++ {
		fields[i] = int32(binary.LittleEndian.Uint32(z.data[i*4 : i*4+4]))
	}
	z.users = int(fields[4])
	return nil
}

func (z *zkClient) getUsers() ([]userRecord, error) {
	if err := z.readSizes(); err != nil {
		return nil, err
	}
	if z.users == 0 {
		return []userRecord{}, nil
	}
	data, err := z.readWithBuffer(cmdUserTempRRQ, fctUser, 0)
	if err != nil {
		return nil, err
	}
	users := parseSDKUserData(data, z.users)
	if len(users) == 0 && len(data) > 4 {
		return nil, errors.New("device returned user data but no records could be parsed")
	}
	return users, nil
}

func (z *zkClient) readWithBuffer(command uint16, fct int32, ext int32) ([]byte, error) {
	maxChunk := 16 * 1024
	if z.tcp {
		maxChunk = 0xFFC0
	}
	body := make([]byte, 11)
	body[0] = 1
	binary.LittleEndian.PutUint16(body[1:3], command)
	binary.LittleEndian.PutUint32(body[3:7], uint32(fct))
	binary.LittleEndian.PutUint32(body[7:11], uint32(ext))

	resp, err := z.sendCommand(cmdReadWithBuffer, body, 1024)
	if err != nil {
		return nil, err
	}
	if !resp.ok {
		return nil, fmt.Errorf("read-with-buffer response %d", resp.code)
	}
	if resp.code == cmdData {
		return append([]byte(nil), z.data...), nil
	}
	size := z.bufferSizeFromData()
	if size <= 0 {
		return nil, errors.New("read-with-buffer returned no size")
	}
	var out []byte
	start := 0
	for start < size {
		chunkSize := maxChunk
		if remaining := size - start; remaining < chunkSize {
			chunkSize = remaining
		}
		chunk, err := z.readChunk(start, chunkSize)
		if err != nil {
			return nil, err
		}
		out = append(out, chunk...)
		start += chunkSize
	}
	_ = z.freeData()
	if len(out) > size {
		out = out[:size]
	}
	return out, nil
}

func (z *zkClient) bufferSizeFromData() int {
	if len(z.data) >= 5 {
		return int(binary.LittleEndian.Uint32(z.data[1:5]))
	}
	if len(z.data) >= 4 {
		return int(binary.LittleEndian.Uint32(z.data[:4]))
	}
	return 0
}

func (z *zkClient) readChunk(start int, size int) ([]byte, error) {
	body := make([]byte, 8)
	binary.LittleEndian.PutUint32(body[0:4], uint32(start))
	binary.LittleEndian.PutUint32(body[4:8], uint32(size))
	resp, err := z.sendCommand(cmdReadChunk, body, size+32)
	if err != nil {
		return nil, err
	}
	if !resp.ok {
		return nil, fmt.Errorf("read chunk response %d", resp.code)
	}
	return z.receiveChunk(size)
}

func (z *zkClient) receiveChunk(expected int) ([]byte, error) {
	if z.response == cmdData {
		if len(z.data) > expected {
			return append([]byte(nil), z.data[:expected]...), nil
		}
		return append([]byte(nil), z.data...), nil
	}
	if z.response != cmdPrepareData {
		return nil, fmt.Errorf("unexpected chunk response %d", z.response)
	}

	size := expected
	if len(z.data) >= 4 {
		size = int(binary.LittleEndian.Uint32(z.data[:4]))
	}
	if size <= 0 || size > expected+1024 {
		size = expected
	}
	var out []byte
	if z.tcp {
		for len(out) < size {
			inner, err := z.readTCPFrame()
			if err != nil {
				return nil, err
			}
			resp, err := z.acceptInnerPacket(inner)
			if err != nil {
				return nil, err
			}
			switch resp.code {
			case cmdData:
				out = append(out, z.data...)
			case cmdAckOK:
				return out, nil
			default:
				return nil, fmt.Errorf("unexpected prepared data frame %d", resp.code)
			}
		}
		if inner, err := z.readTCPFrameWithTimeout(300 * time.Millisecond); err == nil {
			_, _ = z.acceptInnerPacket(inner)
		}
		if len(out) > size {
			out = out[:size]
		}
		return out, nil
	}

	buf := make([]byte, 1032)
	for {
		_ = z.conn.SetReadDeadline(time.Now().Add(z.timeout))
		n, err := z.conn.Read(buf)
		if err != nil {
			return nil, err
		}
		resp, err := z.acceptInnerPacket(buf[:n])
		if err != nil {
			return nil, err
		}
		if resp.code == cmdData {
			out = append(out, z.data...)
			if len(out) >= size {
				return out[:size], nil
			}
			continue
		}
		if resp.code == cmdAckOK {
			return out, nil
		}
		return nil, fmt.Errorf("unexpected UDP chunk response %d", resp.code)
	}
}

func (z *zkClient) freeData() error {
	resp, err := z.sendCommand(cmdFreeData, nil, 8)
	if err != nil {
		return err
	}
	if !resp.ok {
		return fmt.Errorf("free data response %d", resp.code)
	}
	return nil
}

func parseSDKUserData(data []byte, expectedUsers int) []userRecord {
	if len(data) <= 4 {
		return nil
	}
	totalSize := int(binary.LittleEndian.Uint32(data[:4]))
	if totalSize <= 0 || totalSize > len(data)-4 {
		totalSize = len(data) - 4
	}
	recordSize := 0
	if expectedUsers > 0 {
		recordSize = totalSize / expectedUsers
	}
	if recordSize != 28 && recordSize != 72 {
		if totalSize%72 == 0 {
			recordSize = 72
		} else if totalSize%28 == 0 {
			recordSize = 28
		}
	}
	if recordSize != 28 && recordSize != 72 {
		return nil
	}
	return parseFixedUsers(data[4:4+totalSize], recordSize, "sdk")
}

func parseFixedUsers(data []byte, recordSize int, source string) []userRecord {
	var users []userRecord
	for len(data) >= recordSize {
		record := data[:recordSize]
		data = data[recordSize:]
		user, ok := parseFixedUserRecord(record, recordSize)
		if !ok {
			continue
		}
		user.Source = source
		users = append(users, user)
	}
	return dedupeUsers(users)
}

func parseFixedUserRecord(record []byte, recordSize int) (userRecord, bool) {
	switch recordSize {
	case 28:
		if len(record) < 28 {
			return userRecord{}, false
		}
		uid := int(binary.LittleEndian.Uint16(record[0:2]))
		privilege := int(record[2])
		password := trimCString(record[3:8])
		name := trimCString(record[8:16])
		card := binary.LittleEndian.Uint32(record[16:20])
		groupID := int(record[21])
		userID := strconv.FormatUint(uint64(binary.LittleEndian.Uint32(record[24:28])), 10)
		if uid == 0 && (userID == "" || userID == "0") {
			return userRecord{}, false
		}
		if name == "" && userID != "" && userID != "0" {
			name = "NN-" + userID
		}
		if !plausibleUser(uid, userID, name) {
			return userRecord{}, false
		}
		return userRecord{
			UID:       strconv.Itoa(uid),
			UserID:    userID,
			Name:      name,
			Privilege: strconv.Itoa(privilege),
			Password:  password,
			GroupID:   strconv.Itoa(groupID),
			Card:      strconv.FormatUint(uint64(card), 10),
		}, true
	case 72:
		if len(record) < 72 {
			return userRecord{}, false
		}
		uid := int(binary.LittleEndian.Uint16(record[0:2]))
		privilege := int(record[2])
		password := trimCString(record[3:11])
		name := trimCString(record[11:35])
		card := binary.LittleEndian.Uint32(record[35:39])
		groupID := trimCString(record[40:47])
		userID := trimCString(record[48:72])
		if uid == 0 && userID == "" {
			return userRecord{}, false
		}
		if name == "" && userID != "" {
			name = "NN-" + userID
		}
		if !plausibleUser(uid, userID, name) {
			return userRecord{}, false
		}
		return userRecord{
			UID:       strconv.Itoa(uid),
			UserID:    userID,
			Name:      name,
			Privilege: strconv.Itoa(privilege),
			Password:  password,
			GroupID:   groupID,
			Card:      strconv.FormatUint(uint64(card), 10),
		}, true
	default:
		return userRecord{}, false
	}
}

func plausibleUser(uid int, userID string, name string) bool {
	if uid < 0 || uid > 65535 {
		return false
	}
	userID = strings.TrimSpace(userID)
	if userID == "" || userID == "0" {
		return false
	}
	if len(userID) > 32 {
		return false
	}
	if !mostlyPrintable(userID) || !mostlyPrintable(name) {
		return false
	}
	return true
}

func trimCString(raw []byte) string {
	if index := bytes.IndexByte(raw, 0); index >= 0 {
		raw = raw[:index]
	}
	return strings.TrimSpace(validString(raw))
}

func validString(raw []byte) string {
	return strings.ToValidUTF8(string(raw), "")
}

func mostlyPrintable(value string) bool {
	if value == "" {
		return true
	}
	total := 0
	bad := 0
	for _, r := range value {
		total++
		if r == unicode.ReplacementChar || (unicode.IsControl(r) && !unicode.IsSpace(r)) {
			bad++
		}
	}
	return total == 0 || bad*4 <= total
}

func fetchUsersViaTelnet(ip string, cfg config) ([]userRecord, string, error) {
	session, err := newTelnetSession(ip, cfg.TelnetPort, cfg.Timeout, cfg.Verbose)
	if err != nil {
		return nil, "", err
	}
	defer session.close()
	if err := session.login(cfg.TelnetUsername, cfg.TelnetPassword, cfg.TelnetExpectBanner); err != nil {
		return nil, session.transcript.String(), err
	}

	var all []userRecord
	if users, err := telnetSQLiteUsers(session); err == nil {
		all = append(all, users...)
	} else if cfg.Verbose {
		fmt.Printf("    sqlite telnet strategy failed: %v\n", err)
	}
	if users, err := telnetRawFileUsers(session); err == nil {
		all = append(all, users...)
	} else if cfg.Verbose {
		fmt.Printf("    raw-file telnet strategy failed: %v\n", err)
	}
	all = dedupeUsers(all)
	if len(all) == 0 {
		return nil, session.transcript.String(), errors.New("logged in but could not parse users from known ZKT stores")
	}
	for i := range all {
		if all[i].Source == "" {
			all[i].Source = "telnet"
		}
	}
	return all, session.transcript.String(), nil
}

type telnetSession struct {
	conn       net.Conn
	timeout    time.Duration
	verbose    bool
	transcript bytes.Buffer
	counter    int
}

func newTelnetSession(ip string, port int, timeout time.Duration, verbose bool) (*telnetSession, error) {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(ip, strconv.Itoa(port)), timeout)
	if err != nil {
		return nil, err
	}
	return &telnetSession{conn: conn, timeout: timeout, verbose: verbose}, nil
}

func (t *telnetSession) close() {
	if t.conn != nil {
		_, _ = t.conn.Write([]byte("exit\r\n"))
		_ = t.conn.Close()
	}
}

func (t *telnetSession) login(username, password, expectBanner string) error {
	initial, _ := t.readUntilAny([]string{"login:", "Login:", "username:", "Username:", "Password:", "#", "$"}, t.timeout)
	lower := strings.ToLower(initial)
	if strings.Contains(initial, "#") || strings.Contains(initial, "$") {
		return t.verifyBanner(expectBanner)
	}
	if strings.Contains(lower, "login:") || strings.Contains(lower, "username:") {
		if err := t.sendLine(username); err != nil {
			return err
		}
		if _, err := t.readUntilAny([]string{"Password:", "password:"}, t.timeout); err != nil {
			return err
		}
	}
	if err := t.sendLine(password); err != nil {
		return err
	}
	output, err := t.readUntilAny([]string{"#", "$", "Login incorrect", "login:"}, t.timeout)
	if err != nil {
		return err
	}
	if strings.Contains(strings.ToLower(output), "incorrect") {
		return errors.New("telnet login incorrect")
	}
	return t.verifyBanner(expectBanner)
}

func (t *telnetSession) verifyBanner(expectBanner string) error {
	if strings.TrimSpace(expectBanner) == "" {
		return nil
	}
	output, err := t.runCommand("uname -a")
	if err != nil {
		return err
	}
	if !strings.Contains(output, expectBanner) && !strings.Contains(t.transcript.String(), expectBanner) {
		return fmt.Errorf("telnet banner did not contain %q", expectBanner)
	}
	return nil
}

func (t *telnetSession) sendLine(value string) error {
	_, err := t.conn.Write([]byte(value + "\r\n"))
	return err
}

func (t *telnetSession) readUntilAny(markers []string, timeout time.Duration) (string, error) {
	deadline := time.Now().Add(timeout)
	var out strings.Builder
	buf := make([]byte, 1024)
	for time.Now().Before(deadline) {
		_ = t.conn.SetReadDeadline(time.Now().Add(350 * time.Millisecond))
		n, err := t.conn.Read(buf)
		if n > 0 {
			clean := t.cleanTelnetBytes(buf[:n])
			text := string(clean)
			out.WriteString(text)
			t.transcript.WriteString(text)
			current := out.String()
			for _, marker := range markers {
				if strings.Contains(current, marker) {
					return current, nil
				}
			}
		}
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			if errors.Is(err, io.EOF) {
				break
			}
			return out.String(), err
		}
	}
	if out.Len() > 0 {
		return out.String(), nil
	}
	return "", errors.New("telnet read timed out")
}

func (t *telnetSession) cleanTelnetBytes(raw []byte) []byte {
	const (
		iac  = 255
		will = 251
		wont = 252
		do   = 253
		dont = 254
	)
	out := make([]byte, 0, len(raw))
	for i := 0; i < len(raw); i++ {
		b := raw[i]
		if b != iac {
			out = append(out, b)
			continue
		}
		if i+1 >= len(raw) {
			break
		}
		cmd := raw[i+1]
		if cmd == iac {
			out = append(out, iac)
			i++
			continue
		}
		if i+2 < len(raw) && (cmd == will || cmd == wont || cmd == do || cmd == dont) {
			opt := raw[i+2]
			if cmd == do || cmd == dont {
				_, _ = t.conn.Write([]byte{iac, wont, opt})
			} else {
				_, _ = t.conn.Write([]byte{iac, dont, opt})
			}
			i += 2
			continue
		}
		i++
	}
	return out
}

func (t *telnetSession) runCommand(command string) (string, error) {
	t.counter++
	begin := fmt.Sprintf("__ZKT_BEGIN_%d__", t.counter)
	end := fmt.Sprintf("__ZKT_END_%d__", t.counter)
	line := fmt.Sprintf("echo %s; %s 2>&1; echo %s:$?\r\n", begin, command, end)
	if _, err := t.conn.Write([]byte(line)); err != nil {
		return "", err
	}
	output, err := t.readUntilAny([]string{end + ":"}, 20*time.Second)
	if err != nil {
		return output, err
	}
	start := strings.Index(output, begin)
	finish := strings.Index(output, end+":")
	if start >= 0 {
		output = output[start+len(begin):]
	}
	if finish >= 0 {
		output = output[:finish]
	}
	return strings.TrimSpace(stripCommandEcho(output, command)), nil
}

func stripCommandEcho(output string, command string) string {
	lines := strings.Split(output, "\n")
	var kept []string
	for _, line := range lines {
		trimmed := strings.TrimSpace(strings.TrimRight(line, "\r"))
		if trimmed == "" {
			continue
		}
		if strings.Contains(trimmed, command) && strings.Contains(trimmed, "echo __ZKT_BEGIN") {
			continue
		}
		kept = append(kept, line)
	}
	return strings.Join(kept, "\n")
}

func telnetSQLiteUsers(session *telnetSession) ([]userRecord, error) {
	check, err := session.runCommand("command -v sqlite3 >/dev/null 2>&1 && echo YES || echo NO")
	if err != nil {
		return nil, err
	}
	if !strings.Contains(check, "YES") {
		return nil, errors.New("sqlite3 not available on device")
	}
	paths, err := telnetCandidateFiles(session)
	if err != nil {
		return nil, err
	}
	var dbs []string
	for _, path := range paths {
		lower := strings.ToLower(path)
		if strings.HasSuffix(lower, ".db") || strings.Contains(lower, "sqlite") {
			dbs = append(dbs, path)
		}
	}
	var users []userRecord
	var errs []error
	for _, db := range dbs {
		tableOutput, err := session.runCommand(fmt.Sprintf("sqlite3 %s \"SELECT name FROM sqlite_master WHERE type='table';\"", shellQuote(db)))
		if err != nil {
			errs = append(errs, err)
			continue
		}
		for _, table := range strings.Fields(tableOutput) {
			if !likelyUserTable(table) {
				continue
			}
			query := fmt.Sprintf(
				"sqlite3 -header -csv %s \"SELECT * FROM \\\"%s\\\";\"",
				shellQuote(db),
				strings.ReplaceAll(table, "\"", "\"\""),
			)
			csvText, err := session.runCommand(query)
			if err != nil {
				errs = append(errs, err)
				continue
			}
			parsed, err := parseUsersFromCSV(csvText, "telnet-sqlite:"+filepath.Base(db)+":"+table)
			if err != nil {
				errs = append(errs, err)
				continue
			}
			users = append(users, parsed...)
		}
	}
	users = dedupeUsers(users)
	if len(users) == 0 {
		return nil, errors.Join(append(errs, errors.New("no SQLite users parsed"))...)
	}
	return users, nil
}

func likelyUserTable(name string) bool {
	lower := strings.ToLower(name)
	return strings.Contains(lower, "user") ||
		strings.Contains(lower, "person") ||
		strings.Contains(lower, "employee") ||
		lower == "userinfo" ||
		lower == "personnel_employee"
}

func parseUsersFromCSV(text string, source string) ([]userRecord, error) {
	text = cleanCommandCSV(text)
	reader := csv.NewReader(strings.NewReader(text))
	reader.FieldsPerRecord = -1
	rows, err := reader.ReadAll()
	if err != nil {
		return nil, err
	}
	if len(rows) < 2 {
		return nil, nil
	}
	headers := rows[0]
	var users []userRecord
	for _, row := range rows[1:] {
		raw := map[string]string{}
		for i, header := range headers {
			if i < len(row) {
				raw[header] = strings.TrimSpace(row[i])
			}
		}
		user := userRecord{
			UID:       firstByNames(raw, "uid", "userid", "user_id", "id", "pin"),
			UserID:    firstByNames(raw, "badgenumber", "badgenum", "usercode", "user_id", "pin", "enrollnumber", "enroll_no", "enrollno"),
			Name:      firstByNames(raw, "name", "username", "first_name"),
			Privilege: firstByNames(raw, "privilege", "priv", "authority"),
			Password:  firstByNames(raw, "password", "passwd"),
			GroupID:   firstByNames(raw, "group_id", "groupid", "accgroup", "defaultdeptid", "department_id"),
			Card:      firstByNames(raw, "card", "cardno", "card_number", "cardnum"),
			Source:    source,
			Raw:       raw,
		}
		if user.UserID == "" {
			user.UserID = user.UID
		}
		if user.UID == "" {
			user.UID = user.UserID
		}
		if user.UserID == "" && user.Name == "" {
			continue
		}
		users = append(users, user)
	}
	return users, nil
}

func cleanCommandCSV(text string) string {
	lines := strings.Split(text, "\n")
	var kept []string
	for _, line := range lines {
		trimmed := strings.TrimSpace(strings.TrimRight(line, "\r"))
		if trimmed == "" || strings.HasPrefix(trimmed, "__ZKT_") {
			continue
		}
		if strings.Contains(trimmed, "sqlite3 -header -csv") {
			continue
		}
		kept = append(kept, line)
	}
	return strings.Join(kept, "\n")
}

func firstByNames(row map[string]string, names ...string) string {
	normalized := map[string]string{}
	for key, value := range row {
		normalized[normalizeColumnName(key)] = value
	}
	for _, name := range names {
		if value := strings.TrimSpace(normalized[normalizeColumnName(name)]); value != "" {
			return value
		}
	}
	return ""
}

func normalizeColumnName(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	var out strings.Builder
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			out.WriteRune(r)
		}
	}
	return out.String()
}

func telnetRawFileUsers(session *telnetSession) ([]userRecord, error) {
	paths, err := telnetCandidateFiles(session)
	if err != nil {
		return nil, err
	}
	var users []userRecord
	var errs []error
	for _, path := range paths {
		if !likelyRawUserFile(path) {
			continue
		}
		sizeText, _ := session.runCommand(fmt.Sprintf("wc -c < %s 2>/dev/null", shellQuote(path)))
		size, _ := strconv.Atoi(strings.TrimSpace(firstNumber(sizeText)))
		if size <= 0 || size > 8*1024*1024 {
			continue
		}
		data, err := telnetDumpFile(session, path)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		parsed := parseUsersFromBlob(data, "telnet-raw:"+filepath.Base(path))
		users = append(users, parsed...)
	}
	users = dedupeUsers(users)
	if len(users) == 0 {
		return nil, errors.Join(append(errs, errors.New("no raw users parsed"))...)
	}
	return users, nil
}

func telnetCandidateFiles(session *telnetSession) ([]string, error) {
	command := `for d in /mnt /mnt/mtdblock /mnt/mtdblock* /data /home /root /usr /media; do [ -d "$d" ] && find "$d" -type f 2>/dev/null; done | grep -Ei '(user|person|employee|att|zk|sqlite|\.db|\.dat)' | head -n 160`
	output, err := session.runCommand(command)
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var paths []string
	for _, line := range strings.Split(output, "\n") {
		path := strings.TrimSpace(strings.TrimRight(line, "\r"))
		if path == "" || !strings.HasPrefix(path, "/") || seen[path] {
			continue
		}
		seen[path] = true
		paths = append(paths, path)
	}
	if len(paths) == 0 {
		return nil, errors.New("no candidate files found")
	}
	return paths, nil
}

func likelyRawUserFile(path string) bool {
	lower := strings.ToLower(filepath.Base(path))
	return strings.Contains(lower, "user") ||
		strings.Contains(lower, "person") ||
		strings.Contains(lower, "employee") ||
		lower == "userinfo.dat" ||
		lower == "user.dat" ||
		strings.HasSuffix(lower, ".db") ||
		strings.HasSuffix(lower, ".dat")
}

func telnetDumpFile(session *telnetSession, path string) ([]byte, error) {
	quoted := shellQuote(path)
	command := fmt.Sprintf(`if command -v base64 >/dev/null 2>&1; then echo __BASE64__; base64 %s; elif command -v hexdump >/dev/null 2>&1; then echo __HEX__; hexdump -v -e '1/1 "%%02x"' %s; elif command -v xxd >/dev/null 2>&1; then echo __HEX__; xxd -p %s; else echo __NO_DUMP_TOOL__; fi`, quoted, quoted, quoted)
	output, err := session.runCommand(command)
	if err != nil {
		return nil, err
	}
	lines := strings.Split(output, "\n")
	mode := ""
	var body strings.Builder
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		switch trimmed {
		case "__BASE64__":
			mode = "base64"
			continue
		case "__HEX__":
			mode = "hex"
			continue
		case "__NO_DUMP_TOOL__":
			return nil, errors.New("device has no base64/hexdump/xxd")
		}
		if mode != "" {
			body.WriteString(trimmed)
		}
	}
	clean := regexp.MustCompile(`\s+`).ReplaceAllString(body.String(), "")
	switch mode {
	case "base64":
		return base64.StdEncoding.DecodeString(clean)
	case "hex":
		return hex.DecodeString(clean)
	default:
		return nil, errors.New("could not detect dump encoding")
	}
}

func parseUsersFromBlob(data []byte, source string) []userRecord {
	var all []userRecord
	if len(data) > 4 {
		total := int(binary.LittleEndian.Uint32(data[:4]))
		if total > 0 && total <= len(data)-4 {
			if total%72 == 0 {
				all = append(all, parseFixedUsers(data[4:4+total], 72, source)...)
			}
			if total%28 == 0 {
				all = append(all, parseFixedUsers(data[4:4+total], 28, source)...)
			}
		}
	}
	all = append(all, scanFixedUsers(data, 72, source)...)
	all = append(all, scanFixedUsers(data, 28, source)...)
	return dedupeUsers(all)
}

func scanFixedUsers(data []byte, recordSize int, source string) []userRecord {
	best := []userRecord{}
	bestScore := 0
	limit := recordSize
	if len(data) < limit {
		limit = len(data)
	}
	for offset := 0; offset < limit; offset++ {
		var current []userRecord
		for pos := offset; pos+recordSize <= len(data); pos += recordSize {
			user, ok := parseFixedUserRecord(data[pos:pos+recordSize], recordSize)
			if ok {
				user.Source = source
				current = append(current, user)
			}
		}
		score := len(current)
		if score > bestScore {
			bestScore = score
			best = current
		}
	}
	if bestScore == 0 {
		return nil
	}
	return dedupeUsers(best)
}

func firstNumber(value string) string {
	for _, field := range strings.Fields(value) {
		if _, err := strconv.Atoi(field); err == nil {
			return field
		}
	}
	return ""
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func dedupeUsers(users []userRecord) []userRecord {
	seen := map[string]bool{}
	var out []userRecord
	for _, user := range users {
		key := strings.TrimSpace(user.UID) + "|" + strings.TrimSpace(user.UserID)
		if key == "|" {
			key = strings.TrimSpace(user.Name)
		}
		if key == "" || seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, user)
	}
	sort.SliceStable(out, func(i, j int) bool {
		left, lerr := strconv.Atoi(out[i].UserID)
		right, rerr := strconv.Atoi(out[j].UserID)
		if lerr == nil && rerr == nil {
			return left < right
		}
		return out[i].UserID < out[j].UserID
	})
	return out
}

func writeJSONReport(outDir string, report exportReport) (string, error) {
	path := filepath.Join(outDir, fmt.Sprintf("zkt_users_%s.json", timestampForFile()))
	data, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return "", err
	}
	return path, os.WriteFile(path, append(data, '\n'), 0o644)
}

func writeCSVReport(outDir string, report exportReport) (string, error) {
	path := filepath.Join(outDir, fmt.Sprintf("zkt_users_%s.csv", timestampForFile()))
	file, err := os.Create(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	writer := csv.NewWriter(file)
	defer writer.Flush()
	if err := writer.Write([]string{"device_ip", "device_port", "method", "uid", "user_id", "name", "privilege", "password", "group_id", "card", "source"}); err != nil {
		return "", err
	}
	for _, device := range report.Devices {
		for _, user := range device.Users {
			if err := writer.Write([]string{
				device.IP,
				strconv.Itoa(device.Port),
				device.Method,
				user.UID,
				user.UserID,
				user.Name,
				user.Privilege,
				user.Password,
				user.GroupID,
				user.Card,
				user.Source,
			}); err != nil {
				return "", err
			}
		}
	}
	return path, writer.Error()
}

func writeTelnetLog(outDir string, ip string, content string) (string, error) {
	safeIP := strings.ReplaceAll(ip, ":", "_")
	path := filepath.Join(outDir, fmt.Sprintf("zkt_telnet_%s_%s.txt", safeIP, timestampForFile()))
	return path, os.WriteFile(path, []byte(content), 0o600)
}

func timestampForFile() string {
	return time.Now().Format("20060102_150405")
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
