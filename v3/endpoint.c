/* QUIC burst endpoint for the pinned picoquic build. Times are monotonic us. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "picoquic_internal.h"
#include "picoquic_bbr1.h"
#include "picoquic_qlog.h"

#define MAX_TASKS 2048
#define ALPN "gold-pipe-v3"
#define PORT 4433

typedef struct {
    uint64_t offset, size, first_rate, second_rate, step, hold, window, retry;
    uint64_t start, token, received, hash;
    char file[512];
} task_t;

static task_t tasks[MAX_TASKS];
static size_t count, next_task;
static FILE *events, *samples;
static int client, failed;

static uint64_t clock_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static int callback(picoquic_cnx_t *cnx, uint64_t stream, uint8_t *bytes, size_t length,
    picoquic_call_back_event_t event, void *ctx, void *stream_ctx)
{
    (void)ctx;
    (void)stream_ctx;
    if (event != picoquic_callback_stream_data && event != picoquic_callback_stream_fin)
        return 0;
    size_t index = stream / 4;
    if (stream % 4 || index >= MAX_TASKS)
        return -1;
    task_t *task = &tasks[index];
    if (!client) {
        task->received += length;
        for (size_t i = 0; i < length; i++) {
            task->hash ^= bytes[i];
            task->hash *= UINT64_C(1099511628211);
        }
    }
    if (event == picoquic_callback_stream_fin) {
        uint64_t now = clock_us();
        if (client) {
            task->token = now;
            fprintf(events, "%zu,token,%lu,0,0\n", index, now);
        } else {
            fprintf(events, "%zu,upload,%lu,%lu,%lu\n", index, now, task->received, task->hash);
            return picoquic_add_to_stream(cnx, stream, (const uint8_t *)"A", 1, 1);
        }
    }
    return 0;
}

static struct sockaddr_in addr(const char *ip)
{
    struct sockaddr_in result = {.sin_family = AF_INET, .sin_port = htons(PORT)};
    inet_pton(AF_INET, ip, &result.sin_addr);
    return result;
}

static int submit(picoquic_cnx_t *cnx, task_t *task, size_t index, uint64_t now)
{
    if (task->retry) {
        task_t *prev = &tasks[task->retry - 1];
        if (prev->token && prev->token <= prev->start + 20000) {
            fprintf(events, "%zu,skip,%lu,0,0\n", index, now);
            return 0;
        }
    }
    uint8_t *body = calloc(task->size, 1);
    if (!body)
        return -1;
    if (strcmp(task->file, "-")) {
        FILE *input = fopen(task->file, "rb");
        if (!input || fread(body, 1, task->size, input) != task->size) {
            free(body);
            if (input) fclose(input);
            return -1;
        }
        fclose(input);
    }
    int ret = picoquic_add_to_stream(cnx, 4*index, body, task->size, 1);
    free(body);
    task->start = now;
    fprintf(events, "%zu,start,%lu,%lu,0\n", index, now, task->size);
    return ret;
}

int main(int argc, char **argv)
{
    if (argc != 6) return 2;
    client = !strcmp(argv[1], "client");
    if (chdir(argv[2])) return 2;
    uint64_t origin = strtoull(argv[3], NULL, 10), end = origin + strtoull(argv[4], NULL, 10);
    struct sockaddr_in local = addr(client ? "10.206.0.2" : "10.207.0.2");
    struct sockaddr_in peer = addr("10.206.0.1");
    events = fopen(client ? "client.csv" : "server.csv", "w");
    if (!events) return 2;
    fprintf(events, "task,event,us,bytes,hash\n");
    uint64_t sample_due = origin;
    if (client) {
        FILE *plan = fopen("tasks.tsv", "r");
        samples = fopen("samples.csv", "w");
        if (!plan || !samples) return 2;
        fprintf(samples, "us,task,pacing_bps,cwnd,inflight,srtt_us,btlbw_bps,state,lost,delivery_sample_bps\n");
        for (; count < MAX_TASKS; count++) {
            task_t *task = &tasks[count];
            if (fscanf(plan, "%lu %lu %lu %lu %lu %lu %lu %lu %511s", &task->offset,
                &task->size, &task->first_rate, &task->second_rate, &task->step, &task->hold,
                &task->window, &task->retry, task->file) != 9)
                break;
        }
        fclose(plan);
    }
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&local, sizeof(local))) { perror("bind"); return 2; }
    int buffer = 4*1024*1024;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &buffer, sizeof(buffer));
    uint64_t now = clock_us();
    picoquic_quic_t *quic = picoquic_create(2, client ? NULL : "../cert.pem", client ? NULL : "../key.pem",
        client ? "../cert.pem" : NULL, ALPN, callback, NULL, NULL, NULL, NULL, now, &now, NULL, NULL, 0);
    if (!quic) return 2;
    picoquic_set_default_congestion_algorithm(quic, picoquic_bbr1_algorithm);
    picoquic_set_default_pmtud_policy(quic, picoquic_pmtud_blocked);
    if (!strcmp(argv[5], "qlog")) {
        if (picoquic_set_qlog(quic, client ? "qlog-client" : "qlog-server")) return 2;
    }
    picoquic_cnx_t *cnx = NULL;
    if (client) {
        cnx = picoquic_create_cnx(quic, picoquic_null_connection_id, picoquic_null_connection_id,
            (struct sockaddr *)&peer, now, 0, "localhost", ALPN, 1);
        if (!cnx || picoquic_start_client_cnx(cnx)) return 2;
    }
    struct pollfd poller = {.fd = fd, .events = POLLIN};
    uint8_t buf[65536];
    while ((now = clock_us()) < end && !failed) {
        for (;;) {
            struct sockaddr_in from;
            socklen_t len = sizeof(from);
            ssize_t n = recvfrom(fd, buf, sizeof(buf), 0, (struct sockaddr *)&from, &len);
            if (n < 0) break;
            now = clock_us();
            picoquic_incoming_packet(quic, buf, n, (struct sockaddr *)&from,
                (struct sockaddr *)&local, 0, 0, now);
        }
        while (client && next_task < count && origin + tasks[next_task].offset <= now) {
            if (picoquic_get_cnx_state(cnx) != picoquic_state_ready ||
                submit(cnx, &tasks[next_task], next_task, now)) {
                fprintf(stderr, "submit failed at task %zu, state %d\n", next_task, picoquic_get_cnx_state(cnx));
                failed = 1;
                break;
            }
            next_task++;
        }
        int64_t cap_wake = 2000;
        uint64_t selected_rate = 0;
        picoquic_path_t *path = client ? cnx->path[0] : NULL;
        if (client && next_task) {
            task_t *task = &tasks[next_task-1];
            if (task->start && !task->token && now < task->start + task->hold) {
                uint64_t elapsed = now - task->start;
                selected_rate = elapsed < task->step ? task->first_rate : task->second_rate;
                if (elapsed < task->step && task->step - elapsed < (uint64_t)cap_wake)
                    cap_wake = task->step - elapsed;
                if (task->window && path->cwin < task->window) path->cwin = task->window;
            }
        }
        if (selected_rate && picoquic_get_pacing_rate(cnx) != selected_rate)
            picoquic_update_pacing_rate(path, (double)selected_rate, 2*path->send_mtu);
        if (client && now >= sample_due) {
            uint64_t state = 0, bw = 0;
            if (path->congestion_alg_state) picoquic_bbr1_algorithm->alg_observe(path, &state, &bw);
            fprintf(samples, "%lu,%zu,%lu,%lu,%lu,%lu,%lu,%lu,%lu,%lu\n", now,
                next_task, 8*picoquic_get_pacing_rate(cnx), path->cwin, path->bytes_in_transit,
                path->smoothed_rtt, 8*bw, state, cnx->nb_retransmission_total, 8*path->bandwidth_estimate);
            sample_due = now + 1000;
        }
        for (int i = 0; i < 32; i++) {
            size_t len = 0;
            struct sockaddr_storage to, from;
            int index = 0;
            picoquic_connection_id_t cid;
            picoquic_cnx_t *last = NULL;
            now = clock_us();
            int ret = picoquic_prepare_next_packet(quic, now, buf, 1536, &len, &to, &from, &index, &cid, &last);
            if (ret || !len) break;
            if (sendto(fd, buf, len, 0, (struct sockaddr *)&to, sizeof(struct sockaddr_in)) != (ssize_t)len) {
                perror("sendto");
                failed = 1;
                break;
            }
        }
        int64_t wait = picoquic_get_next_wake_delay(quic, now, cap_wake);
        if (client) {
            if (next_task < count && (int64_t)(origin + tasks[next_task].offset - now) < wait)
                wait = (int64_t)(origin + tasks[next_task].offset - now);
            if ((int64_t)(sample_due - now) < wait) wait = (int64_t)(sample_due - now);
        }
        if (wait > 0) {
            struct timespec pause = {0, wait*1000};
            ppoll(&poller, 1, &pause, NULL);
        }
    }
    fclose(events);
    if (samples) fclose(samples);
    picoquic_free(quic);
    close(fd);
    return failed || (client && next_task != count);
}
