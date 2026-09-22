# Task one, verify access: not completed, and why

The first task was to make an authenticated call against the Karbon API using
credentials Carl provides, and to stop and report back if access is not
available. No call was made. Two separate things blocked it, and both need
Carl before this can be closed.

## No credentials were provided

The session was to begin with a Karbon API access key or workspace token with
scope to read work items, clients and webhook configuration. Nothing of the
kind reached this session. There is no Karbon credential in the environment,
and inventing one to produce a green tick would be worse than useless.

## The network this ran on cannot reach Karbon

Independently of the credentials, outbound traffic from this environment goes
through an egress proxy with a host allowlist, and both `api.karbonhq.com` and
`developers.karbonhq.com` are refused at the CONNECT stage with a 403. So even
with a valid key in hand, the call could not have completed from here. It will
complete from Carl's own machine or from wherever the runner is eventually
hosted, since that is an ordinary internet connection.

## What was done instead

The verification is written, tested and ready to run. It lives in
`runner.karbon_client.KarbonClient.verify_access` and is exposed as:

```
python -m runner.cli verify-access
```

It makes one authenticated GET against `/v3/TenantSettings`, which is the right
probe because it is cheap, strictly read only, and a success also returns the
tenant key, the configured work types and the configured work statuses, all of
which the task two audit needs anyway. The command writes its outcome to the
audit log either way, so the attempt itself is on the record.

## Reading the result honestly

The interesting failure is a 401, and a 401 does not by itself mean the plan
excludes API access. Karbon authenticates with two headers that are issued
together and are not interchangeable, an `Authorization: Bearer` application
token and an `AccessKey`, so the most common cause of a 401 is the two values
being swapped or one of them having been regenerated. The command says exactly
this rather than jumping to a conclusion about the plan.

The plan level answer is not found in a response code at all. It is found in
the Karbon web application under Settings, then Connected Apps. If Carl can see
an API application there, or can create one, the plan includes API access and
any failure is a credential or scope problem. If that section is absent or
refuses to create an application, that is the answer, and it is the point at
which this phase stops rather than building further.

A 403 is a third case and the command separates it out: the credentials were
accepted and the application is authenticated, but it lacks permission for the
call. That is a scope problem, fixed by widening the application's permissions,
not by changing plan.

## What Carl needs to do

Run the two commands below from a machine with ordinary internet access, with
the two credentials in the environment.

```
export KARBON_BEARER_TOKEN=...      # the application token, a GUID
export KARBON_ACCESS_KEY=...        # from Settings, Connected Apps
python -m runner.cli verify-access
```

If that prints a tenant key, task one is done and task two can run immediately
with `python -m runner.cli audit-karbon`. If it prints a 401 or a 403, the
message tells you which of the three cases above you are in.
