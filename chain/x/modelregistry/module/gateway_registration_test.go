package modelregistry

import (
	"context"
	"errors"
	"github.com/cosmos/cosmos-sdk/client"
	gateway "github.com/grpc-ecosystem/grpc-gateway/runtime"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestGatewayRegistersParamsRoute(t *testing.T) {
	conn, err := grpc.NewClient("passthrough:///offline", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return nil, errors.New("offline") }))
	require.NoError(t, err)
	defer conn.Close()
	mux := gateway.NewServeMux()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	ask := func() int {
		rr := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/dendra/modelregistry/v1/params", nil).WithContext(ctx)
		mux.ServeHTTP(rr, req)
		return rr.Code
	}
	require.Equal(t, http.StatusNotFound, ask())
	AppModule{}.RegisterGRPCGatewayRoutes(client.Context{CmdContext: context.Background(), GRPCClient: conn}, mux)
	require.NotEqual(t, http.StatusNotFound, ask())
}
